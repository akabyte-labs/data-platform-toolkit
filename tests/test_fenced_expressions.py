"""
Fenced expressions (```) next to other declarations.

The first version of the server recognized a delimiter only at the start of
a line: it missed the opening one after `measure X =`, took the closing one
for an opening one and read every declaration after it as part of the
expression, so looking up the next measure returned "not found". The
opening delimiter is recognized only right after the '=' of a declaration
or of an expression property, as the TMDL specification requires.
"""

from __future__ import annotations

import io

import pytest

from mcp_data_platform_toolkit.semantic_model import model_loader as loader
from mcp_data_platform_toolkit.semantic_model import reader_tools, writer_tools
from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _declarations(lines: list[str]) -> list[tuple[str, str]]:
    return [(b.keyword, b.name) for b in tmdl.find_all_blocks(lines)]


ADJACENT_LAYOUTS = {
    "as reported": "measure Prima = ```\n        1\n        ```\nmeasure Seconda = 2\n",
    "in a table": "table T\n\tmeasure Prima = ```\n\t\t\t1\n\t\t\t```\n\tmeasure Seconda = 2\n",
    "closing fence at column 0": "table T\n\tmeasure Prima = ```\n\t\t\t1\n```\n\tmeasure Seconda = 2\n",
    "quoted name with '='": "table T\n\tmeasure 'Prima = 1' = ```\n\t\t\t1\n\t\t\t```\n\tmeasure Seconda = 2\n",
}


@pytest.mark.parametrize("text", ADJACENT_LAYOUTS.values(), ids=ADJACENT_LAYOUTS.keys())
def test_the_measure_after_a_fenced_one_is_found(text):
    lines = _lines(text)
    first = tmdl.find_all_blocks(lines)[-2]
    second = tmdl.find_block(lines, "Seconda", "measure")
    assert tmdl.get_expression(lines, first) == "1"
    assert first.end == second.start
    assert tmdl.get_expression(lines, second) == "2"


def test_adjacent_fenced_measures_keep_their_own_body_and_properties():
    lines = _lines(
        "table T\n"
        "\tmeasure A = ```\n\t\t\tVAR x = 1\n\t\t\tRETURN x\n\t\t\t```\n"
        "\tmeasure B = ```\n\t\t\t[A] + 1\n\t\t\t```\n\t\tformatString: 0\n"
        "\tmeasure C = ```\n\t\t\t[B] * 2\n\t\t\t```\n"
        "\tcolumn D\n\t\tdataType: int64\n"
    )
    assert _declarations(lines) == [
        ("table", "T"), ("measure", "A"), ("measure", "B"), ("measure", "C"), ("column", "D")]
    blocks = {b.name: b for b in tmdl.find_all_blocks(lines)}
    assert tmdl.get_expression(lines, blocks["A"]) == "VAR x = 1\nRETURN x"
    assert tmdl.get_expression(lines, blocks["B"]) == "[A] + 1"
    assert tmdl.get_expression(lines, blocks["C"]) == "[B] * 2"
    assert tmdl.get_property(lines, blocks["A"], "formatString") is None
    assert tmdl.get_property(lines, blocks["B"], "formatString") is not None
    assert [c.name for c in tmdl.iter_children(lines, blocks["T"]) if c.kind == "object"] == ["A", "B", "C", "D"]


def test_fenced_expression_properties_end_at_their_closing_fence():
    lines = _lines(
        "table T\n"
        "\tmeasure A = 1\n\t\tformatStringDefinition = ```\n\t\t\t\t\"0.0%\"\n\t\t\t\t```\n"
        "\tmeasure B = 2\n"
        "\tpartition P = m\n\t\tmode: import\n\t\tsource = ```\n\t\t\t\tlet\n\t\t\t\tin x\n\t\t\t\t```\n"
        "\tmeasure C = 3\n"
    )
    assert _declarations(lines) == [
        ("table", "T"), ("measure", "A"), ("measure", "B"), ("partition", "P"), ("measure", "C")]
    first = tmdl.find_block(lines, "A", "measure")
    updated, changed = tmdl.set_expression_property(lines, first, "formatStringDefinition", '"0%"')
    assert changed
    assert _declarations(updated) == _declarations(lines)
    assert "```" not in "".join(updated[first.start:tmdl.find_block(updated, "B", "measure").start])


UNFENCED_BODIES = {
    "DAX comment": "table T\n\tmeasure A =\n\t\t\tVAR x = 1 // see ```\n\t\t\tRETURN x\n\tmeasure B = 2\n",
    "inline expression": "table T\n\tmeasure A = 1 // x = ```\n\tmeasure B = 2\n",
    "M source": "table T\n\tpartition P = m\n\t\tsource =\n\t\t\t\tlet s = 1 // ```\n\t\t\t\tin s\n\tmeasure B = 2\n",
}


@pytest.mark.parametrize("text", UNFENCED_BODIES.values(), ids=UNFENCED_BODIES.keys())
def test_a_line_that_only_ends_with_a_fence_does_not_open_one(text):
    lines = _lines(text)
    assert tmdl.fenced_mask(lines) == [tmdl.LINE_NORMAL] * len(lines)
    assert tmdl.find_block(lines, "B", "measure").start == len(lines) - 1


def test_the_measure_after_a_fenced_one_can_be_read_and_documented(write_files, tmp_path):
    root = write_files({
        "M.pbip": {"artifacts": []},
        "M.SemanticModel/definition/model.tmdl": "model Model\n\tculture: en-US\n",
        "M.SemanticModel/definition/tables/Sales.tmdl": (
            "table Sales\n"
            "\tmeasure Prima = ```\n\t\t\t1\n\t\t\t```\n"
            "\tmeasure Seconda = ```\n\t\t\t[Prima] + 1\n\t\t\t```\n"
            "\tmeasure Terza = [Seconda] * 2\n"
            "\tcolumn Amount\n\t\tdataType: double\n"),
    }, str(tmp_path / "workspace"))

    assert "[Prima] + 1" in reader_tools.get_dax_measure(root, "Sales", "Seconda")
    assert "Sales[Seconda]" in reader_tools.get_dax_lineage(root, "Terza")

    writer_tools.write_smart_description(root, "Sales", "Seconda", "Second measure.")
    writer_tools.write_smart_description(root, "Sales", "Terza", "Third measure.")
    _path, _fingerprint, lines, block = loader.locate_element(root, "Sales", "Terza", ["measure"])
    assert tmdl.get_doc_comments(lines, block) == ["\t/// Third measure."]
    assert tmdl.get_expression(lines, block) == "[Seconda] * 2"

    with io.open(loader.get_table_file_path(root, "Sales"), encoding="utf-8", newline="") as handle:
        text = handle.read()
    assert text.count("```") == 4
    assert "\t/// Second measure.\r\n\tmeasure Seconda = ```\r\n" in text


def test_a_description_with_a_fence_is_refused_and_an_existing_one_is_kept(write_files, tmp_path):
    root = write_files({
        "M.pbip": {"artifacts": []},
        "M.SemanticModel/definition/model.tmdl": "model Model\n\tculture: en-US\n",
        "M.SemanticModel/definition/tables/Sales.tmdl": (
            "table Sales\n"
            "\t/// Written elsewhere with ``` in it\n"
            "\tmeasure Base = 1\n"
            "\tcolumn Amount\n\t\tdataType: double\n"),
    }, str(tmp_path / "workspace"))
    path = loader.get_table_file_path(root, "Sales")
    with io.open(path, encoding="utf-8") as handle:
        before = handle.read()

    with pytest.raises(tmdl.TmdlError, match="three backticks"):
        writer_tools.create_measure(root, "Sales", "New", "1", description="See ```")
    with pytest.raises(tmdl.TmdlError, match="three backticks"):
        writer_tools.write_smart_description(root, "Sales", "Amount", "Amount ```", element_type="column")
    with io.open(path, encoding="utf-8") as handle:
        assert handle.read() == before

    # 'merge' keeps an existing description as it is, backticks included.
    writer_tools.write_smart_description(root, "Sales", "Base", "Base measure.", mode="merge")
    _path, _fingerprint, lines, block = loader.locate_element(root, "Sales", "Base", ["measure"])
    assert tmdl.get_doc_comments(lines, block) == ["\t/// Written elsewhere with ``` in it",
                                                   "\t/// Base measure."]
