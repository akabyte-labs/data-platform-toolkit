"""
The first description ever written asks the user whether they have rules of
their own: in a form when the client can show one, otherwise through the
assistant. Once per user, and never when they already have rules.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp_types import ElicitResult

from mcp_data_platform_toolkit.mcp_common import user_folder
from mcp_data_platform_toolkit.semantic_model import description_rules as rules
from mcp_data_platform_toolkit.semantic_model import reader_tools, writer_tools
from tests.self_test import create_pbip_project

WRITE = {"table_name": "Sales", "element_name": "Total Sales",
         "base_description": "Revenue from all sales.", "mode": "overwrite"}


@pytest.fixture
def project(tmp_path, monkeypatch, guidelines_already_asked):
    """A PBIP project, with a user who was never asked for their rules."""
    monkeypatch.setattr(rules, "needs_guidelines_question", guidelines_already_asked)
    return create_pbip_project(str(tmp_path / "pbip"))


def _tmdl(project) -> str:
    from mcp_data_platform_toolkit.semantic_model.model_loader import get_table_file_path
    with open(get_table_file_path(project, "Sales"), encoding="utf-8") as handle:
        return handle.read()


def _call(arguments: dict, answers=(), asked: list | None = None, tool: str = "write_smart_description"):
    """Calls a tool through an in-process client; `answers` play the user filling the forms."""
    queue = list(answers)

    async def callback(_context, params):
        if asked is not None:
            asked.append(params.message)
        return queue.pop(0)

    async def run():
        mcp = MCPServer("question")
        reader_tools.register_reader_tools(mcp)
        writer_tools.register_writer_tools(mcp)
        async with Client(mcp, elicitation_callback=callback if answers else None) as client:
            result = await client.call_tool(tool, arguments)
            return result.is_error, "\n".join(block.text for block in result.content if hasattr(block, "text"))
    return asyncio.run(run())


def _folder_answer():
    return ElicitResult(action="accept", content={"location": "settings", "folder": ""})


def _form_answer(text):
    return ElicitResult(action="accept", content={"rules": text})


def _is_question(message: str) -> bool:
    return "explicit rules for the descriptions" in message


def test_the_answer_is_never_a_parameter_the_model_fills():
    async def schema():
        mcp = MCPServer("question")
        writer_tools.register_writer_tools(mcp)
        async with Client(mcp) as client:
            tools = (await client.list_tools()).tools
            return next(t for t in tools if t.name == "write_smart_description").input_schema
    properties = asyncio.run(schema()).get("properties", {})
    assert "user_guidelines" not in properties and "choice" not in properties


def test_without_forms_the_first_write_stops_and_the_assistant_asks(project):
    before = _tmdl(project)
    error, text = _call({"project_path": project, **WRITE})
    assert not error and text.startswith("Nothing was written") and "ask the user" in text
    assert "save_description_guidelines" in text
    assert _tmdl(project) == before
    # Asked once: the next call writes.
    error, text = _call({"project_path": project, **WRITE})
    assert not error and "Smart-Doc" in text and "Revenue from all sales." in _tmdl(project)


def test_rules_given_in_the_form_are_saved_and_the_write_waits(project):
    asked: list = []
    error, text = _call({"project_path": project, **WRITE},
                        [_folder_answer(), _form_answer("Write in British English.")], asked)
    assert not error and text.startswith("Nothing was written")
    assert sum(_is_question(m) for m in asked) == 1
    general = user_folder.user_folder() / rules.RULES_FILE
    assert "Write in British English." in general.read_text(encoding="utf-8")
    assert "Write in British English." in reader_tools.get_description_guidelines(project)
    assert "Revenue from all sales." not in _tmdl(project)
    error, text = _call({"project_path": project, **WRITE}, [_form_answer("never used")], asked)
    assert not error and "Smart-Doc" in text
    assert sum(_is_question(m) for m in asked) == 1, "the question is asked only once"


PREVIEW = "get_smart_description_preview"


def test_the_preview_asks_first_and_the_write_does_not_ask_again(project):
    error, text = _call({"project_path": project, **WRITE}, tool=PREVIEW)
    assert not error and text.startswith("Nothing was written") and "ask the user" in text
    assert "Status:" not in text
    error, text = _call({"project_path": project, **WRITE}, tool=PREVIEW)
    assert not error and "Status:" in text, "asked once: the preview goes on"
    error, text = _call({"project_path": project, **WRITE})
    assert not error and "Revenue from all sales." in _tmdl(project), "the write is not stopped again"


def test_rules_given_in_the_form_before_the_preview_are_saved(project):
    asked: list = []
    error, text = _call({"project_path": project, **WRITE},
                        [_folder_answer(), _form_answer("Write in British English.")], asked, tool=PREVIEW)
    assert not error and text.startswith("Nothing was written")
    assert sum(_is_question(m) for m in asked) == 1
    assert "Write in British English." in (user_folder.user_folder() / rules.RULES_FILE).read_text(encoding="utf-8")
    error, text = _call({"project_path": project, **WRITE}, [_form_answer("never used")], asked, tool=PREVIEW)
    assert not error and "Status:" in text and sum(_is_question(m) for m in asked) == 1


def test_the_preview_of_a_missing_table_does_not_ask(project):
    error, _text = _call({"project_path": project, **{**WRITE, "table_name": "Nope"}}, tool=PREVIEW)
    assert error
    assert rules.needs_guidelines_question()


def test_the_answer_is_never_a_parameter_of_the_preview():
    async def schema():
        mcp = MCPServer("question")
        reader_tools.register_reader_tools(mcp)
        async with Client(mcp) as client:
            tools = (await client.list_tools()).tools
            return next(t for t in tools if t.name == PREVIEW).input_schema
    assert "user_guidelines" not in asyncio.run(schema()).get("properties", {})


def test_an_empty_answer_or_a_refusal_writes_at_once(project):
    error, text = _call({"project_path": project, **WRITE}, [_folder_answer(), _form_answer("  ")])
    assert not error and "Smart-Doc" in text and "Revenue from all sales." in _tmdl(project)
    assert user_folder.setting(rules.GUIDELINES_ASKED) is True


def test_a_declined_form_counts_as_asked(project):
    error, text = _call({"project_path": project, **WRITE},
                        [_folder_answer(), ElicitResult(action="decline")])
    assert not error and "Smart-Doc" in text
    assert not rules.needs_guidelines_question()


def test_a_user_with_rules_already_is_not_asked(project):
    rules.save_rules("Call the customers 'clients'.", "general")
    asked: list = []
    error, text = _call({"project_path": project, **WRITE}, [_folder_answer()], asked)
    assert not error and "Smart-Doc" in text
    assert not any(_is_question(m) for m in asked)


def test_the_question_is_settled_in_the_settings_of_the_user(project):
    writer_tools.write_smart_description(project, **WRITE)
    assert user_folder.setting(rules.GUIDELINES_ASKED) is True
    assert not rules.needs_guidelines_question()
