"""
What a lineage did not cover must be impossible to miss: the reports left out of
it (PBIR-Legacy) are flagged at the top of the end-to-end lineage and counted in
its Coverage line, and the server tells the assistant, when it connects, to say
so and not to rebuild a lineage by hand.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import Client

from mcp_data_platform_toolkit import server
from mcp_data_platform_toolkit.lineage import lineage_tools
from tests.test_end_to_end import FILES

LEGACY = {"bi/Old.Report/report.json": {"sections": [{"visualContainers": [{"config": "{}"}]}]}}
ELSEWHERE = {"bi/Other.Report/definition.pbir": {"version": "4.0", "datasetReference": {
    "byPath": {"path": "../Other.SemanticModel"}}},
    "bi/Other.Report/definition/pages/p1/page.json": {"displayName": "Page"}}


def _end_to_end(write_files, tmp_path, extra: dict) -> str:
    root = write_files({**FILES, **extra}, str(tmp_path / "ws"))
    return lineage_tools.get_end_to_end_lineage(
        os.path.join(root, "bi", "Shop.pbip"), sql_project_path=os.path.join(root, "db"),
        target="[Ratio]", direction="upstream")


def test_a_legacy_report_is_flagged_at_the_top_and_counted(write_files, tmp_path):
    text = _end_to_end(write_files, tmp_path, LEGACY)
    header = text.split("\n\n")[0]
    assert "[!] 1 report(s) NOT analyzed" in header and "Old: PBIR-Legacy format, not analyzed" in header
    assert "upgrade them to PBIR" in header
    coverage = next(line for line in text.splitlines() if line.startswith("Coverage:"))
    assert "1 report(s) NOT analyzed: their visuals are not in these numbers." in coverage


def test_with_every_report_analyzed_nothing_is_flagged(write_files, tmp_path):
    text = _end_to_end(write_files, tmp_path, {})
    assert "NOT analyzed" not in text and "Reports analyzed: Shop" in text


def test_a_report_of_another_model_is_not_a_gap(write_files, tmp_path):
    text = _end_to_end(write_files, tmp_path, ELSEWHERE)
    assert "NOT analyzed" not in text
    assert "Reports of another model, not part of this one: Other: bound to the model" in text


def test_an_export_names_the_reports_it_leaves_out(write_files, tmp_path):
    root = write_files({**FILES, **LEGACY}, str(tmp_path / "ws"))
    text = lineage_tools.export_lineage(
        "markdown", project_path=os.path.join(root, "bi", "Shop.pbip"), sql_project_path=os.path.join(root, "db"))
    assert "NOT analyzed" in text and "Old" in text


def test_the_assistant_is_told_what_to_say_when_it_connects(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mcp-data-platform-toolkit", "--access=read_only", "--msft-bridge=off"])
    mcp = server.build_server()

    async def instructions():
        async with Client(mcp) as client:
            return client.instructions
    text = asyncio.run(instructions())
    assert text == server.SERVER_INSTRUCTIONS
    for must in ("Do not rebuild a lineage", "'Coverage' line", "NOT analyzed", "PROVISIONAL",
                 "get_description_guidelines"):
        assert must in text
    assert len(text) < 2000, "read at every start: keep it short"
