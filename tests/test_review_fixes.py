"""
Regressions of the second review round, as isolated pytest tests: each one
builds its own project and reproduces a defect observed on the earlier code.
"""

from __future__ import annotations

import io
import os

import pytest

from mcp_data_platform_toolkit.mcp_common import output_shaping
from mcp_data_platform_toolkit.powerbi_bridge import bridge
from mcp_data_platform_toolkit.reports.pbir_references import MATCH_BINDING, MATCH_TEXT
from mcp_data_platform_toolkit.reports.report_tools import collect_field_usage, read_report_binding
from mcp_data_platform_toolkit.semantic_model import model_loader as loader
from mcp_data_platform_toolkit.semantic_model import ols_roles, reader_tools, writer_tools
from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.project_paths import PathNotAllowedError, find_definition_folder


def _read(path: str) -> str:
    with io.open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# TMDL writing
# ---------------------------------------------------------------------------

def test_new_expression_keeps_the_first_line_indentation(demo_project):
    """`.strip()` removed the first line's own indentation: the body came back misaligned."""
    writer_tools.create_measure(demo_project, "Sales", "Indented", "        SUM(Sales[Amount])\n    + 1")
    _path, _fingerprint, lines, block = loader.locate_element(demo_project, "Sales", "Indented", ["measure"])
    assert tmdl.get_expression(lines, block) == "    SUM(Sales[Amount])\n+ 1"


def test_a_deeper_comment_line_is_not_the_next_description():
    """A '///' line ending the previous expression became the description of the next object."""
    lines = [line + "\n" for line in (
        "table T",
        "\tmeasure A =",
        "\t\t\t1",
        "\t\t\t/// trailing comment of A",
        "\t/// Description of B",
        "\tmeasure B = 2",
    )]
    block = tmdl.find_block(lines, "B", "measure")
    assert tmdl.get_doc_comments(lines, block) == ["\t/// Description of B"]


def test_true_booleans_are_written_as_power_bi_desktop_writes_them(demo_project):
    """'isHidden: true' is valid, but Desktop rewrites it as 'isHidden' on its next save."""
    writer_tools.set_item_visibility(demo_project, "Sales", "Total Sales", "measure", True)
    writer_tools.create_measure(demo_project, "Sales", "Hidden One", "1", is_hidden=True)
    text = _read(loader.get_table_file_path(demo_project, "Sales"))
    assert "isHidden: true" not in text
    _path, _fingerprint, lines, block = loader.locate_element(demo_project, "Sales", "Hidden One", ["measure"])
    assert tmdl.is_hidden(lines, block)
    assert any(line.strip() == "isHidden" for line in lines[block.start:block.end])


# ---------------------------------------------------------------------------
# OLS
# ---------------------------------------------------------------------------

def test_an_existing_role_with_nothing_to_change_is_not_rewritten(demo_project):
    """It was rewritten identical and reported as 'updated'."""
    ols_roles.set_ols_permission(demo_project, "Readers", model_permission="read")
    roles = os.path.join(find_definition_folder(demo_project), "roles.tmdl")
    before = (_read(roles), os.stat(roles).st_mtime_ns)

    message = ols_roles.set_ols_permission(demo_project, "Readers")
    assert message.startswith("No changes")
    assert (_read(roles), os.stat(roles).st_mtime_ns) == before

    assert "updated" in ols_roles.set_ols_permission(demo_project, "Readers", model_permission="none")


# ---------------------------------------------------------------------------
# Bridge detail retrieval
# ---------------------------------------------------------------------------

def test_result_detail_is_bounded():
    """Any budget was accepted, and a negative offset sliced from the end of the list."""
    items = [{"name": f"item {i}", "expression": "x" * 500} for i in range(100)]
    result_id = output_shaping.store_result("t", {"items": items}, "{}")
    text = bridge.get_official_result_detail(result_id, "items", offset=-5, limit=0, max_chars=10**9)
    assert "items 0-1 of 100" in text
    # 50,000 characters asked for in one response: held to the server's budget
    # (plus the header and the reduction note).
    huge = bridge.get_official_result_detail(result_id, "items", limit=100, max_chars=10**9)
    assert len(huge) <= output_shaping.MAX_TOOL_OUTPUT_CHARS + 400


# ---------------------------------------------------------------------------
# Report binding: one reader of definition.pbir
# ---------------------------------------------------------------------------

def test_report_binding_reads_both_forms(write_files):
    root = write_files({
        "Local.Report/definition.pbir": {"datasetReference": {"byPath": {"path": "../M.SemanticModel"}}},
        "Live.Report/definition.pbir": {"datasetReference": {"byConnection": {
            "connectionString": "Data Source=powerbi://api.powerbi.com/v1.0/myorg/WS;Initial Catalog=Published"}}},
    })
    local = read_report_binding(os.path.join(root, "Local.Report"))
    live = read_report_binding(os.path.join(root, "Live.Report"))
    assert (local["mode"], local["model_name"]) == ("byPath", "M")
    assert local["model_folder"] == os.path.join(root, "M.SemanticModel")
    assert (live["mode"], live["model_name"]) == ("byConnection", "Published")


# ---------------------------------------------------------------------------
# MCP_ALLOWED_ROOTS
# ---------------------------------------------------------------------------

TABLE = "table T\n\tcolumn C\n\t\tdataType: string\n"


@pytest.fixture
def two_areas(write_files, tmp_path):
    """An allowed area with a project, and a model outside it."""
    allowed = write_files({"Inside.SemanticModel/definition/tables/T.tmdl": TABLE}, str(tmp_path / "allowed"))
    outside = write_files({"Outside.SemanticModel/definition/tables/T.tmdl": TABLE}, str(tmp_path / "outside"))
    return allowed, outside


def test_without_allowed_roots_everything_is_reachable(two_areas, monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_ROOTS", raising=False)
    _allowed, outside = two_areas
    assert "T" in reader_tools.list_tables(outside).splitlines()


def test_allowed_roots_refuse_a_project_outside_them(two_areas, monkeypatch, tmp_path):
    allowed, outside = two_areas
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", os.pathsep.join([allowed, str(tmp_path / "elsewhere")]))
    assert "T" in reader_tools.list_tables(allowed).splitlines()
    with pytest.raises(PathNotAllowedError):
        reader_tools.list_tables(outside)
    with pytest.raises(PathNotAllowedError):
        writer_tools.create_measure(outside, "T", "M", "1")


def test_allowed_roots_follow_a_pbip_to_its_model(two_areas, monkeypatch, write_files):
    """A .pbip inside the roots can name a report bound to a model outside them."""
    allowed, outside = two_areas
    write_files({
        "Escape.pbip": {"artifacts": [{"report": {"path": "Escape.Report"}}]},
        "Escape.Report/definition.pbir": {"datasetReference": {"byPath": {
            "path": os.path.relpath(os.path.join(outside, "Outside.SemanticModel"),
                                    os.path.join(allowed, "Escape.Report"))}}},
    }, allowed)
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", allowed)
    with pytest.raises(PathNotAllowedError):
        reader_tools.list_tables(os.path.join(allowed, "Escape.pbip"))


def test_allowed_roots_keep_the_report_scan_from_reading_a_model_outside(write_files, tmp_path, monkeypatch):
    """The model behind a report is read to trace hierarchy levels: not when it is outside the roots."""
    allowed = str(tmp_path / "allowed")
    write_files({"Model.SemanticModel/definition/tables/Date.tmdl": (
        "table Date\n\tcolumn 'Calendar Year'\n\t\tdataType: int64\n"
        "\thierarchy Calendar\n\t\tlevel Year\n\t\t\tcolumn: 'Calendar Year'\n")}, str(tmp_path / "outside"))
    write_files({
        "R.Report/definition.pbir": {"datasetReference": {"byPath": {"path": "../../outside/Model.SemanticModel"}}},
        "R.Report/definition/pages/p1/visuals/v1/visual.json": {"visual": {"visualType": "table", "query": {
            "Select": [{"HierarchyLevel": {"Expression": {"Hierarchy": {
                "Expression": {"SourceRef": {"Entity": "Date"}}, "Hierarchy": "Calendar"}}, "Level": "Year"}}]}}},
    }, allowed)
    target = [{"kind": "column", "table": "Date", "name": "Year"}]

    monkeypatch.delenv("MCP_ALLOWED_ROOTS", raising=False)
    assert not collect_field_usage(allowed, target)["usages"]["Date[Year]"]    # traced to 'Calendar Year'

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", allowed)
    usages = collect_field_usage(allowed, target)["usages"]["Date[Year]"]
    assert [u["match"] for u in usages] == [MATCH_TEXT]                       # model not read: to verify
    assert MATCH_BINDING not in {u["match"] for u in usages}
