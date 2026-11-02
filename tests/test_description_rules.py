"""
The user's own folder and their rules for the descriptions: where the
folder is (the first-time choice, the default, MCP_USER_FOLDER), the files
of a project and of its repository, how the rules add to the built-in ones,
and the tools and the form that change them.

Every test runs with the settings folder and Documents in a temporary
folder (the `private_user_folder` fixture of conftest.py).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp_types import ElicitResult

from mcp_data_platform_toolkit.mcp_common import user_folder
from mcp_data_platform_toolkit.semantic_model import description_rules as rules
from mcp_data_platform_toolkit.semantic_model import reader_tools, writer_tools
from mcp_data_platform_toolkit.semantic_model.description_guidelines import load_full_guidelines
from tests.self_test import create_pbip_project


@pytest.fixture
def project(tmp_path):
    """A PBIP project inside a Git repository called 'contoso-bi'."""
    repo = tmp_path / "contoso-bi"
    (repo / ".git").mkdir(parents=True)
    return create_pbip_project(str(repo / "pbip"))


def _settings() -> dict:
    return json.loads((user_folder.default_folder() / user_folder.SETTINGS_FILE).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The folder
# ---------------------------------------------------------------------------

def test_the_default_folder_is_the_settings_folder_of_the_system(private_user_folder):
    expected = (private_user_folder / "AppData" / "Roaming" if os.name == "nt"
                else private_user_folder / ".config") / "data-platform-toolkit"
    assert user_folder.default_folder() == expected
    assert user_folder.needs_choice() and user_folder.user_folder() == expected


def test_without_a_choice_the_default_is_used_and_remembered(project):
    text = reader_tools.get_description_guidelines(project)
    folder = user_folder.default_folder()
    assert "First use: your files (rules for the descriptions, lineage configurations)" in text
    assert str(folder) in text and "--user-folder" in text
    assert _settings() == {"folder": str(folder)}
    assert (folder / rules.RULES_FILE).read_text(encoding="utf-8").startswith("# My rules")
    # Asked once: the second call says nothing about it.
    again = reader_tools.get_description_guidelines(project)
    assert "First use" not in again and not user_folder.needs_choice()


def test_the_environment_variable_wins_and_is_never_overwritten(project, tmp_path, monkeypatch):
    folder = tmp_path / "office files"
    monkeypatch.setenv("MCP_USER_FOLDER", str(folder))
    text = reader_tools.get_description_guidelines(project)
    assert "First use" not in text and str(folder / rules.RULES_FILE) in text
    assert not (user_folder.default_folder() / user_folder.SETTINGS_FILE).exists()
    for change in (lambda: user_folder.choose_folder(tmp_path / "x"),
                   lambda: user_folder.move_folder(tmp_path / "x")):
        with pytest.raises(ValueError, match="MCP_USER_FOLDER"):
            change()


# ---------------------------------------------------------------------------
# The first-time question, through an MCP client
# ---------------------------------------------------------------------------

def _server(full_access: bool = False) -> MCPServer:
    mcp = MCPServer("rules")
    reader_tools.register_reader_tools(mcp)
    if full_access:
        writer_tools.register_writer_tools(mcp)
    return mcp


def _call(tool: str, arguments: dict, answer=None, asked: list | None = None, full_access=False):
    """Calls a tool through an in-process client; `answer` plays the user filling the form."""
    async def callback(_context, params):
        if asked is not None:
            asked.append(params.message)
        return answer

    async def run():
        async with Client(_server(full_access), elicitation_callback=callback if answer else None) as client:
            result = await client.call_tool(tool, arguments)
            return result.is_error, "\n".join(block.text for block in result.content if hasattr(block, "text"))
    return asyncio.run(run())


def test_the_choice_is_never_a_parameter_the_model_fills():
    async def schemas():
        async with Client(_server(full_access=True)) as client:
            return {tool.name: tool.input_schema for tool in (await client.list_tools()).tools}
    found = asyncio.run(schemas())
    for name in ("get_description_guidelines", "get_smart_description_preview", "save_description_guidelines"):
        assert "choice" not in found[name].get("properties", {}), name


def test_a_client_without_forms_gets_the_default(project):
    error, text = _call("get_description_guidelines", {"project_path": project})
    assert not error and "First use" in text
    assert _settings()["folder"] == str(user_folder.default_folder())


def test_the_user_chooses_documents_in_the_form_once(project):
    asked: list = []
    answer = ElicitResult(action="accept", content={"location": "documents", "folder": ""})
    error, text = _call("get_description_guidelines", {"project_path": project}, answer, asked)
    documents = user_folder.documents_folder()
    assert not error and "as you chose" in text and str(documents) in text
    assert len(asked) == 1 and str(user_folder.default_folder()) in asked[0] and str(documents) in asked[0]
    assert _settings()["folder"] == str(documents)
    assert (documents / rules.RULES_FILE).is_file()
    _call("get_description_guidelines", {"project_path": project}, answer, asked)
    assert len(asked) == 1, "the question is asked only the first time"


def test_the_user_types_another_folder(project, tmp_path):
    folder = tmp_path / "my files"
    answer = ElicitResult(action="accept", content={"location": "other", "folder": str(folder)})
    error, text = _call("get_smart_description_preview",
                        {"project_path": project, "table_name": "Sales", "element_name": "Total Sales"}, answer)
    assert not error and text.startswith("Rules in use: built-in; yours: " + str(folder / rules.RULES_FILE))
    assert "as you chose" in text and user_folder.user_folder() == folder


def test_an_unusable_folder_or_a_refusal_keeps_the_default(project):
    answer = ElicitResult(action="accept", content={"location": "other", "folder": "relative/path"})
    _error, text = _call("get_description_guidelines", {"project_path": project}, answer)
    assert "can't be used" in text and "not an absolute path" in text
    assert user_folder.user_folder() == user_folder.default_folder()

    (user_folder.default_folder() / user_folder.SETTINGS_FILE).unlink()
    _error, text = _call("get_description_guidelines", {"project_path": project}, ElicitResult(action="decline"))
    assert "First use" in text and _settings()["folder"] == str(user_folder.default_folder())


# ---------------------------------------------------------------------------
# The files and how they add up
# ---------------------------------------------------------------------------

def test_each_project_has_a_folder_named_after_its_repository(project, tmp_path):
    info = rules.project_rules(project)
    assert info.name == "contoso-bi" and info.repo_root == tmp_path / "contoso-bi"
    assert rules.project_file(info) == (user_folder.user_folder() / "projects" / "contoso-bi"
                                        / "description_guidelines.md")
    alone = create_pbip_project(str(tmp_path / "no git" / "Sales 2026"))
    assert rules.project_rules(alone).repo_root is None
    assert rules.project_rules(alone).name == "Sales 2026"
    # A name Windows would refuse as a folder (no folder made of it here).
    assert user_folder.safe_file_name("Sales: 2026") == "Sales_ 2026"


def test_rules_add_up_from_the_most_general(project, tmp_path):
    reader_tools.get_description_guidelines(project)           # first use: the default folder
    info = rules.project_rules(project)
    rules.general_file().write_text("# Mine\n\n- Write in British English.\n", encoding="utf-8")
    (tmp_path / "contoso-bi" / "description_guidelines.md").write_text(
        "<!-- shared -->\n- Customers are 'clients'.\n", encoding="utf-8")
    rules.project_file(info).parent.mkdir(parents=True, exist_ok=True)
    rules.project_file(info).write_text("- The fiscal year starts in October.\n", encoding="utf-8")

    text = reader_tools.get_description_guidelines(project)
    builtin = load_full_guidelines()
    order = [text.index(part) for part in (builtin[:40], "Write in British English", "clients", "fiscal year")]
    assert order == sorted(order), "built-in, yours, the repository's, the project's"
    assert "<!-- shared -->" not in text and "the more specific one wins" in text
    assert "the repository's: " + str(tmp_path / "contoso-bi" / "description_guidelines.md") in text

    rules.project_file(info).write_text("<!-- replace -->\n- Only this.\n", encoding="utf-8")
    text = reader_tools.get_description_guidelines(project)
    assert "Only this." in text and builtin[:40] not in text and "Write in British English" not in text.split("\n", 1)[1]
    assert "replaces the rules before it" in text


def test_an_untouched_template_adds_no_rules(project):
    text = reader_tools.get_description_guidelines(project)
    first_line = text.splitlines()[0]
    assert "yours: " in first_line and "(no rules yet)" in first_line
    assert "(not created yet)" not in first_line               # the project's file is created too
    assert first_line.count("(no rules yet)") == 2
    assert "=== RULES:" not in text


def test_the_project_folder_is_always_created(project, tmp_path):
    reader_tools.get_description_guidelines(project)
    folder = user_folder.user_folder() / user_folder.PROJECTS_FOLDER / "contoso-bi"
    assert (folder / rules.RULES_FILE).read_text(encoding="utf-8").startswith("# Rules for the descriptions of contoso-bi")
    # The lineage configuration of a database project gets its folder too.
    from mcp_data_platform_toolkit.sql_project.config import personal_config_file
    other = tmp_path / "fabrikam-dwh"
    (other / ".git").mkdir(parents=True)
    path = Path(personal_config_file(str(other), str(other / "Fabrikam.sqlproj")))
    assert path.parent.is_dir() and path.parent.name == "fabrikam-dwh" and not path.exists()


def test_no_project_folder_before_the_folder_is_chosen(project):
    from mcp_data_platform_toolkit.sql_project.config import personal_config_file
    personal_config_file(project, "Contoso.sqlproj")
    assert not user_folder.default_folder().exists()


# ---------------------------------------------------------------------------
# Saving and moving
# ---------------------------------------------------------------------------

def test_saving_rules_for_the_project_for_everyone_and_for_the_team(project, tmp_path):
    message = writer_tools.save_description_guidelines("- Say the currency of every amount.", project_path=project)
    project_file = rules.project_file(rules.project_rules(project))
    assert message.startswith(f"Created '{project_file}' (project rules).")
    writer_tools.save_description_guidelines("- Round to two decimals.", project_path=project)
    saved = project_file.read_text(encoding="utf-8")
    assert saved.startswith("# Rules for the descriptions of contoso-bi")
    assert saved.index("currency") < saved.index("two decimals")

    writer_tools.save_description_guidelines("- Only this.", project_path=project, mode="overwrite")
    assert project_file.read_text(encoding="utf-8") == "- Only this.\n"

    writer_tools.save_description_guidelines("- Write in British English.", scope="general")
    assert "Write in British English" in rules.general_file().read_text(encoding="utf-8")

    message = writer_tools.save_description_guidelines("- Customers are 'clients'.", scope="repo", project_path=project)
    shared = tmp_path / "contoso-bi" / "description_guidelines.md"
    assert shared.is_file() and str(shared) in message
    assert "clients" in reader_tools.get_description_guidelines(project)


def test_rules_that_cant_be_saved(project, tmp_path, monkeypatch):
    for kwargs, match in (({"rules": "   ", "project_path": project}, "empty"),
                          ({"rules": "a\x07b", "project_path": project}, "control characters"),
                          ({"rules": "x" * 20_001, "project_path": project}, "at most"),
                          ({"rules": "x", "scope": "team", "project_path": project}, "'scope'"),
                          ({"rules": "x", "mode": "replace", "project_path": project}, "'mode'"),
                          ({"rules": "x", "scope": "project"}, "project_path")):
        with pytest.raises(ValueError, match=match):
            rules.save_rules(**kwargs)
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path / "elsewhere"))
    with pytest.raises(ValueError, match="outside the folders"):
        rules.save_rules("x", "repo", project)


def test_moving_the_folder_between_the_two_standard_ones(project):
    writer_tools.save_description_guidelines("- Mine.", scope="general")
    writer_tools.save_description_guidelines("- The project's.", project_path=project)
    lineage = user_folder.default_folder() / "projects" / "contoso-dwh" / "Dwh.lineage.yaml"
    user_folder.write_text(lineage, "version: 1\n")
    documents = user_folder.documents_folder()
    already = documents / "projects" / "contoso-bi" / "description_guidelines.md"
    user_folder.write_text(already, "- Already there.\n")

    message = writer_tools.set_user_folder("documents")
    assert "Moved 'description_guidelines.md'" in message and "Moved 'projects/contoso-dwh/Dwh.lineage.yaml'" in message
    assert "Kept '" in message and user_folder.user_folder() == documents
    assert "Mine." in (documents / rules.RULES_FILE).read_text(encoding="utf-8")
    assert already.read_text(encoding="utf-8") == "- Already there.\n"
    assert (user_folder.default_folder() / "projects" / "contoso-bi" / "description_guidelines.md").is_file(), \
        "the file not moved stays where it was"
    assert "already in" in writer_tools.set_user_folder("documents")
    with pytest.raises(ValueError, match="'location'"):
        writer_tools.set_user_folder(str(Path.home()))


def test_the_tools_respect_the_access_mode():
    async def names(full_access):
        async with Client(_server(full_access)) as client:
            return {tool.name for tool in (await client.list_tools()).tools}
    read_only, full = asyncio.run(names(False)), asyncio.run(names(True))
    assert "get_description_guidelines" in read_only
    for writer in ("save_description_guidelines", "set_user_folder"):
        assert writer not in read_only and writer in full


@pytest.mark.skipif(os.name != "nt", reason="the known-folder API exists only on Windows")
def test_windows_finds_the_real_documents_folder():
    # The other tests replace documents_folder(): this one runs the real
    # lookup, which follows OneDrive's folder backup.
    found = user_folder._windows_documents()
    assert found is not None and found.is_absolute() and found.is_dir()
