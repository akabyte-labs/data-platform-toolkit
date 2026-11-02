"""
The server as a client starts it (Claude Desktop, Claude Code): its own
process, talking MCP over stdio. The other tests call the tools in the same
process; these check that the forms, the hidden parameters and the warning of
a draft survive the real transport.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from mcp import Client, StdioServerParameters
from mcp_types import ElicitResult

from mcp_data_platform_toolkit.lineage import config_review, draft_notice
from mcp_data_platform_toolkit.mcp_common import user_folder
from mcp_data_platform_toolkit.sql_project import config as lineage_config
from tests.test_config_draft import DWH


@pytest.fixture
def project(write_files, tmp_path, private_user_folder):
    # The folder is chosen already: the server process reads the same settings.
    user_folder.choose_folder(user_folder.default_folder())
    return write_files(DWH, str(tmp_path / "dwh"))


def _server_env(home) -> dict:
    env = {**os.environ, "MCP_MSFT_BRIDGE": "off", "HOME": str(home), "USERPROFILE": str(home)}
    env.pop("MCP_ACCESS_MODE", None)
    return env


def _session(home, calls, answers=()):
    """Runs `calls(client)` against the server started as its own process; `answers` fill the forms."""
    queue, asked = list(answers), []

    async def callback(_context, params):
        asked.append(params.message)
        return queue.pop(0)

    async def run():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "mcp_data_platform_toolkit", "--access=full_access"],
                                       env=_server_env(home))
        async with Client(params, read_timeout_seconds=60,
                          elicitation_callback=callback if answers else None) as client:
            return await calls(client)
    return asyncio.run(run()), asked


def _text(result) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def test_the_hidden_parameters_stay_hidden_over_stdio(project, private_user_folder):
    async def calls(client):
        return {t.name: t.input_schema.get("properties", {}) for t in (await client.list_tools()).tools}
    tools, _asked = _session(private_user_folder, calls)
    assert "save_lineage_config" in tools and "draft_lineage_config" in tools
    assert not {"review", "choice"} & set(tools["save_lineage_config"])


def test_a_draft_saved_over_stdio_shows_the_warning_and_stays_a_draft(project, private_user_folder):
    async def calls(client):
        draft = _text(await client.call_tool("draft_lineage_config", {"sql_project_path": project}))
        content = draft.split("```yaml\n", 1)[1].split("```", 1)[0]
        saved = await client.call_tool("save_lineage_config",
                                       {"sql_project_path": project, "content": content})
        checked = await client.call_tool("test_lineage_config", {"sql_project_path": project})
        return content, saved, _text(checked)
    (content, saved, checked), asked = _session(
        private_user_folder, calls, [ElicitResult(action="accept", content={"open_in_editor": False})])
    assert "draft: true" in content
    assert asked == [config_review.review_message()], "the user sees the warning as it is"
    assert not saved.is_error and "The user was shown the warning" in _text(saved)
    mine = lineage_config.personal_config_file(project, os.path.join(project, "Dwh.sqlproj"))
    assert os.path.isfile(mine) and lineage_config.load_config(mine).draft
    assert draft_notice.DRAFT_IN_USE in checked


def test_without_forms_the_warning_reaches_the_assistant_over_stdio(project, private_user_folder):
    async def calls(client):
        draft = _text(await client.call_tool("draft_lineage_config", {"sql_project_path": project}))
        content = draft.split("```yaml\n", 1)[1].split("```", 1)[0]
        return await client.call_tool("save_lineage_config", {"sql_project_path": project, "content": content})
    saved, asked = _session(private_user_folder, calls)
    assert asked == [] and not saved.is_error
    assert f"Tell the user, as it is: {draft_notice.DRAFT_WARNING}" in _text(saved)
