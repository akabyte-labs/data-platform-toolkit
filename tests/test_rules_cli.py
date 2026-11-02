"""
The terminal commands for the user's own files: --guidelines opens the
rules for the descriptions, --lineage-config the lineage configuration of a
database project, --user-folder moves the folder. The answers of the user
are scripted, and no editor is ever opened.
"""

from __future__ import annotations

import sys

import pytest

from mcp_data_platform_toolkit import rules_cli, server
from mcp_data_platform_toolkit.mcp_common import user_folder
from mcp_data_platform_toolkit.semantic_model import description_rules as rules
from tests.self_test import create_pbip_project
from tests.test_sql_lineage import CONFIG, PROJECT


def _script(*answers):
    """An `input` that answers in order, and the lines printed."""
    pending, printed = list(answers), []
    return (lambda prompt="": pending.pop(0)), printed.append, printed


@pytest.fixture
def opened(monkeypatch):
    """The files the commands would open in the editor."""
    files = []
    monkeypatch.setattr(rules_cli, "open_in_editor", lambda path: files.append(path) or True)
    return files


@pytest.fixture
def database(write_files, tmp_path):
    """A database project at the root of the repository 'contoso-dwh'."""
    root = write_files(PROJECT, str(tmp_path / "contoso-dwh"))
    (tmp_path / "contoso-dwh" / ".git").mkdir()
    return root


# ---------------------------------------------------------------------------
# --guidelines
# ---------------------------------------------------------------------------

def test_guidelines_asks_the_folder_the_first_time_and_opens_the_file(opened):
    ask, say, printed = _script("2")
    assert rules_cli.run_guidelines(ask=ask, say=say, interactive=True) == 0
    documents = user_folder.documents_folder()
    assert user_folder.user_folder() == documents and opened == [documents / rules.RULES_FILE]
    assert any("Your rules: " in line for line in printed)
    # Asked once: the second time it opens the file directly.
    ask, say, printed = _script()
    assert rules_cli.run_guidelines(ask=ask, say=say, interactive=True) == 0
    assert not any("Where should" in line for line in printed)


def test_guidelines_without_a_terminal_keeps_the_default(opened):
    ask, say, _printed = _script()
    assert rules_cli.run_guidelines(ask=ask, say=say, interactive=False) == 0
    assert user_folder.user_folder() == user_folder.default_folder()


def test_guidelines_of_a_project_creates_its_file_from_the_template(opened, tmp_path):
    (tmp_path / "fabrikam" / ".git").mkdir(parents=True)
    project = create_pbip_project(str(tmp_path / "fabrikam" / "bi"))
    ask, say, printed = _script()
    assert rules_cli.run_guidelines(project, ask=ask, say=say, interactive=False) == 0
    path = user_folder.default_folder() / "projects" / "fabrikam" / "description_guidelines.md"
    assert opened == [path] and path.read_text(encoding="utf-8").startswith("# Rules for the descriptions of fabrikam")
    assert f"Created '{path}' from its template." in printed


def test_guidelines_says_where_the_file_is_when_no_editor_opens(monkeypatch):
    monkeypatch.setattr(rules_cli, "open_in_editor", lambda path: False)
    ask, say, printed = _script()
    rules_cli.run_guidelines(ask=ask, say=say, interactive=False)
    assert printed[-1].startswith("Open it with any text editor")


# ---------------------------------------------------------------------------
# --lineage-config
# ---------------------------------------------------------------------------

def test_lineage_config_creates_yours_from_a_draft_of_the_project(opened, database):
    ask, say, printed = _script()
    assert rules_cli.run_lineage_config(database, ask=ask, say=say, interactive=False) == 0
    path = user_folder.default_folder() / "projects" / "contoso-dwh" / "Shop.lineage.yaml"
    assert opened == [path] and "layers:" in path.read_text(encoding="utf-8")
    assert any(line.startswith(f"Created '{path}' from a draft of the project.") for line in printed)
    # The warning comes before the file is opened, and only when the file was generated.
    assert printed.index(rules_cli.LINEAGE_DRAFT_WARNING) < printed.index(f"Your lineage configuration: {path}")
    assert not (user_folder.default_folder().parent / "contoso-dwh" / "lineage.yaml").exists()
    # The second time it opens the same file, without drafting it again.
    path.write_text("version: 1\nlayers:\n  bronze: raw\n", encoding="utf-8")
    ask, say, printed = _script()
    rules_cli.run_lineage_config(database, ask=ask, say=say, interactive=False)
    assert opened[-1] == path and printed[-1] == f"Your lineage configuration: {path}"
    assert rules_cli.LINEAGE_DRAFT_WARNING not in printed
    assert rules_cli.LINEAGE_STILL_DRAFT not in printed, "a file without 'draft: true' is checked"
    assert path.read_text(encoding="utf-8") == "version: 1\nlayers:\n  bronze: raw\n"


def test_lineage_config_reminds_that_the_file_in_use_is_still_a_draft(opened, database):
    ask, say, printed = _script()
    rules_cli.run_lineage_config(database, ask=ask, say=say, interactive=False)
    path = user_folder.default_folder() / "projects" / "contoso-dwh" / "Shop.lineage.yaml"
    assert "draft: true" in path.read_text(encoding="utf-8")
    ask, say, printed = _script()
    rules_cli.run_lineage_config(database, ask=ask, say=say, interactive=False)
    assert rules_cli.LINEAGE_STILL_DRAFT in printed and rules_cli.LINEAGE_DRAFT_WARNING not in printed


def test_lineage_config_opens_the_repository_one_when_there_is_one(opened, database, tmp_path):
    shared = tmp_path / "contoso-dwh" / "lineage.yaml"
    shared.write_text(CONFIG, encoding="utf-8")
    ask, say, printed = _script()
    assert rules_cli.run_lineage_config(database, ask=ask, say=say, interactive=False) == 0
    assert opened == [shared] and "it wins over yours" in printed[-1]


def test_lineage_config_needs_the_database_project():
    ask, say, printed = _script()
    assert rules_cli.run_lineage_config("", ask=ask, say=say) == 1
    assert "--lineage-config" in printed[-1]


# ---------------------------------------------------------------------------
# --user-folder
# ---------------------------------------------------------------------------

def test_user_folder_moves_the_files_to_the_folder_given(tmp_path):
    user_folder.choose_folder(user_folder.default_folder())
    rules.general_file().write_text("- Mine.\n", encoding="utf-8")
    target = tmp_path / "Shared files"
    ask, say, printed = _script()
    assert rules_cli.run_user_folder(str(target), ask=ask, say=say, interactive=False) == 0
    assert (target / rules.RULES_FILE).read_text(encoding="utf-8") == "- Mine.\n"
    assert user_folder.user_folder() == target and printed[-1] == f"Your files are now in '{target}'."


def test_user_folder_menu(tmp_path):
    target = tmp_path / "typed"
    # A wrong number, a relative folder, then a whole path.
    ask, say, printed = _script("7", "3", "relative", "3", str(target))
    assert rules_cli.run_user_folder(ask=ask, say=say, interactive=True) == 0
    assert user_folder.user_folder() == target
    assert "Answer with a number from 1 to 4." in printed
    assert any(line.startswith("Give the whole path") for line in printed)

    ask, say, printed = _script("")                             # Enter: leave them where they are
    assert rules_cli.run_user_folder(ask=ask, say=say, interactive=True) == 0
    assert user_folder.user_folder() == target and printed[-1] == "Nothing moved."


def test_user_folder_without_a_terminal_only_explains():
    ask, say, printed = _script()
    assert rules_cli.run_user_folder(ask=ask, say=say, interactive=False) == 0
    assert user_folder.needs_choice() and "--user-folder" in printed[-1]


def test_user_folder_set_by_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_USER_FOLDER", str(tmp_path / "office"))
    ask, say, printed = _script()
    assert rules_cli.run_user_folder(str(tmp_path / "x"), ask=ask, say=say) == 1
    assert "change it there" in printed[-1]


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command, function", [("--guidelines", "run_guidelines"),
                                               ("--lineage-config", "run_lineage_config"),
                                               ("--user-folder", "run_user_folder")])
def test_the_command_line_reaches_the_commands(monkeypatch, command, function):
    calls = []
    monkeypatch.setattr(rules_cli, function, lambda argument="": calls.append(argument) or 0)
    monkeypatch.setattr(sys, "argv", ["mcp-data-platform-toolkit", command, "C:\\x\\P"])
    with pytest.raises(SystemExit) as stopped:
        server.main()
    assert stopped.value.code == 0 and calls == ["C:\\x\\P"]


def test_check_says_where_the_files_are(capsys):
    server.run_check()
    out = capsys.readouterr().out
    assert (f"Your folder (rules for the descriptions, lineage configurations): "
            f"{user_folder.default_folder()} (not chosen yet") in out
    assert '"data-platform-toolkit"' in out and '"uvx"' in out and '"mcp-data-platform-toolkit"' in out
    assert '"--python"' in out and f'"{server.UVX_PYTHON}"' in out


def test_the_python_for_uvx_is_the_one_ci_tests():
    """uvx, 'uv run' (.python-version) and CI run the server on the same Python."""
    import os

    import yaml

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".python-version"), encoding="utf-8") as handle:
        pinned = handle.read().strip()
    with open(os.path.join(root, ".github", "workflows", "tests.yml"), encoding="utf-8") as handle:
        tested = yaml.safe_load(handle)["jobs"]["tests"]["strategy"]["matrix"]["python-version"]
    assert server.UVX_PYTHON == pinned and pinned in tested
