"""
get_documentation_coverage: how much of the model has a '///' description,
visible elements apart from hidden ones, for the whole model or one table.
"""

from __future__ import annotations

import os
import re

import pytest

from mcp_data_platform_toolkit.semantic_model import reader_tools
from mcp_data_platform_toolkit.semantic_model.documentation_coverage import documentation_coverage
from mcp_data_platform_toolkit.semantic_model.model_loader import load_semantic_model
from tests.test_desktop_layout import FILES
from tests.test_model_reading import make_project  # noqa: F401  (the fixture)

MODEL = "Contoso.SemanticModel/definition"

# A hidden fact table with three undocumented columns and one documented measure,
# and a visible table with one documented and two undocumented measures, one
# documented and one undocumented column, and a documented hierarchy.
FACTS = """table Facts
	isHidden
	lineageTag: aaaaaaaa-0000-4000-8000-000000000001

	/// Quantity shipped.
	measure 'Shipped' = SUM(Facts[Qty])
		lineageTag: aaaaaaaa-0000-4000-8000-000000000002

	column Qty
		dataType: int64
		sourceColumn: Qty
		lineageTag: aaaaaaaa-0000-4000-8000-000000000003

	column Price
		dataType: double
		sourceColumn: Price
		lineageTag: aaaaaaaa-0000-4000-8000-000000000004

	column Hidden
		isHidden
		dataType: double
		sourceColumn: Hidden
		lineageTag: aaaaaaaa-0000-4000-8000-000000000005

	partition Facts = m
		mode: import
		source = let Source = 1 in Source
"""

PLAIN = """table Plain
	lineageTag: bbbbbbbb-0000-4000-8000-000000000001

	/// The number of orders.
	measure 'Orders' = COUNTROWS(Plain)
		lineageTag: bbbbbbbb-0000-4000-8000-000000000002

	measure 'Refunds' = 0
		lineageTag: bbbbbbbb-0000-4000-8000-000000000003

	measure 'Returns' = 0
		lineageTag: bbbbbbbb-0000-4000-8000-000000000004

	/// The order number.
	column Number
		dataType: int64
		sourceColumn: Number
		lineageTag: bbbbbbbb-0000-4000-8000-000000000005

	column Region
		dataType: string
		sourceColumn: Region
		lineageTag: bbbbbbbb-0000-4000-8000-000000000006

	/// Number, then region.
	hierarchy Place
		lineageTag: bbbbbbbb-0000-4000-8000-000000000007

		level Number
			column: Number
			lineageTag: bbbbbbbb-0000-4000-8000-000000000008

		level Region
			column: Region
			lineageTag: bbbbbbbb-0000-4000-8000-000000000009

	partition Plain = m
		mode: import
		source = let Source = 1 in Source
"""


@pytest.fixture
def project(make_project):  # noqa: F811  (the fixture, used by name)
    return make_project({f"{MODEL}/tables/Facts.tmdl": FACTS, f"{MODEL}/tables/Plain.tmdl": PLAIN})


def _figures(text: str, title: str) -> tuple[int, int, int, int]:
    """(visible documented, visible total, hidden documented, hidden total) of a row of the totals."""
    line = next(line for line in text.splitlines() if line.startswith(title))
    cells = re.findall(r"(\d+) of (\d+) \(\d+%\)|none", line)
    pairs = [(int(done), int(total)) if done else (0, 0) for done, total in cells]
    assert len(pairs) == 2, line
    return pairs[0][0], pairs[0][1], pairs[1][0], pairs[1][1]


def test_the_hidden_table_is_counted_apart_from_the_visible_ones(project, write_files, tmp_path):
    baseline = os.path.join(write_files(FILES, str(tmp_path / "baseline")), "Contoso.pbip")
    before, after = documentation_coverage(baseline), documentation_coverage(project)
    for title, delta in (
            # Plain adds 3 visible measures (1 documented), Facts 1 hidden documented measure.
            ("Measures", (1, 3, 1, 1)),
            # Plain adds 2 visible columns (1 documented), Facts 3 hidden undocumented ones.
            ("Columns", (1, 2, 0, 3)),
            # Plain: its table, 1 hierarchy (documented); Facts: its hidden table.
            ("Tables, hierarchies and calculation items", (1, 2, 0, 1))):
        figures = [a - b for a, b in zip(_figures(after, title), _figures(before, title), strict=True)]
        assert tuple(figures) == delta, title


def test_one_table_lists_the_names_missing(project):
    text = documentation_coverage(project, "Plain")
    assert "(table Plain)" in text
    assert "Visible elements of 'Plain' without a description (4 of 7):" in text
    assert text.endswith("  table: Plain\n  column: Region\n  measure: Refunds\n  measure: Returns")


def test_a_hidden_table_is_said_to_be_hidden(project):
    text = documentation_coverage(project, "Facts")
    assert "'Facts' is a hidden table: all its elements count as hidden." in text
    assert "without a description" not in text


def test_the_tables_are_ranked_by_what_they_miss(project):
    text = documentation_coverage(project)
    ranking = [line.strip() for line in text.split("most missing first:")[1].splitlines()
               if line.startswith("  ")]
    assert "Plain: 4 of 7 visible (2 measures, 1 column, 1 table description)" in ranking
    missing = [int(re.search(r": (\d+) of", line).group(1)) for line in ranking]
    assert missing == sorted(missing, reverse=True)
    assert all(not line.startswith("Facts") for line in ranking), "a hidden table has nothing visible to miss"


def test_every_table_counts_like_get_table_summary(project):
    """The figures of a table are the ones its summary gives."""
    checked = 0
    for name in load_semantic_model(project):
        summary = reader_tools.get_table_summary(project, name)
        documented, visible = map(int, re.search(r"Documented \('///'\): (\d+) of (\d+) visible", summary).groups())
        text = documentation_coverage(project, name)
        found = re.search(r"without a description \((\d+) of (\d+)\)", text)
        missing, total = (int(found.group(1)), int(found.group(2))) if found else (0, visible)
        assert (visible - documented, visible) == (missing, total), name
        checked += visible > 0
    assert checked >= 4


def test_an_unknown_table_is_an_error(project):
    with pytest.raises(FileNotFoundError):
        documentation_coverage(project, "Nowhere")


def test_the_tool_answers_through_the_server(project):
    text = reader_tools.get_documentation_coverage(project)
    assert text.startswith("=== DOCUMENTATION COVERAGE: Contoso.SemanticModel (")
    assert "Visible, all together" in text
