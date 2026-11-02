"""
Self-test with no external test framework: builds a synthetic PBIP project
and a synthetic PBIR report workspace in a temporary folder and exercises
the tools.

Usage:
    python tests/self_test.py

Covers in particular these regressions:
  - TMDL properties written with ':' instead of '='
  - a hidden column making the whole table "hidden"
  - quoted measure names with spaces truncated in the lineage
  - new elements inserted outside the table block
  - fact tables inferred from relationships, not from the presence of measures

It also covers Object-Level Security, the description guidelines, report
usage of measures and columns, and the MCP contract of the tools.

Sections [17]-[25] guard the regressions of the security and robustness
review. Each one reproduces a defect that was observed on earlier code, not
a theoretical risk:
  - properties merged on one line in a file without a trailing newline
  - TMDL property injection through a line break in a value or a name
  - table/role names escaping the project folder
  - concurrent writes silently erasing someone else's change
  - a UTF-8 BOM making the 'table' block invisible
  - a RecursionError in a single file wiping out the whole report scan
  - strings cut at the "full" rung, declared complete
  - a textual match presented as a certain usage in impact analysis
  - the semantic model re-read and re-parsed several times per call

Sections [26]-[33] guard the regressions of the lineage review:
  - fenced expressions hiding the rest of the file, phantom declarations
    read from inside expression bodies
  - wrong DAX references (RETURN/NOT, -- comments, case, non-ASCII tables,
    unqualified columns, calculation items)
  - duplicate measure names across tables accepted
  - technical analysis written into business descriptions, 'merge' = 'append'
  - report usages never seen (PBIR-Legacy, report-level measures, bookmarks,
    drillthrough, hierarchy levels, date variations) or from another model
  - relationship cardinality, table names that aren't file names, bridge paths

Sections [36]-[47] guard the regressions of the review that preceded the
end-to-end lineage:
  - a ``` line or control characters in an expression or a description
  - a .pbip file that didn't identify its own model
  - qualified DAX references to objects that don't exist, DAX constants
    taken for tables, expressions continued after the declaration line
  - properties written where TOM has none (calculation items, tables)
  - fact-table protection without override, OLS race and names
  - bridge: display folders refused as paths, nested failures, timeouts
  - output shaping, report bindings, partial declarations, diagnostics
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model import autodoc
from mcp_data_platform_toolkit.semantic_model import dax_lineage as lineage
from mcp_data_platform_toolkit.semantic_model import model_loader as loader
from mcp_data_platform_toolkit.semantic_model import project_paths as paths
from mcp_data_platform_toolkit.semantic_model import relationships as rels
from mcp_data_platform_toolkit.semantic_model import tmdl_io
from mcp_data_platform_toolkit.semantic_model import ols_roles
from mcp_data_platform_toolkit.semantic_model import reader_tools
from mcp_data_platform_toolkit.semantic_model import writer_tools
from mcp_data_platform_toolkit.reports.report_tools import find_measure_usage_in_reports

FAILED: list[str] = []
PASSED = 0


def check(condition: bool, description: str, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {description}")
    else:
        FAILED.append(description)
        print(f"  FAIL {description}")
        if detail:
            print("       " + detail.replace("\n", "\n       "))


SALES_TABLE = """/// Sales facts
table Sales
\tlineageTag: 11111111-1111-1111-1111-111111111111

\tmeasure 'Total Sales' = SUM(Sales[Amount])
\t\tformatString: #,##0

\tmeasure 'Margin %' =
\t\t\tDIVIDE(
\t\t\t    [Total Sales] - [Total Cost],
\t\t\t    [Total Sales]
\t\t\t)
\t\tformatString: 0.00%
\t\tdisplayFolder: KPI

\tmeasure 'Total Cost' = SUM(Sales[Cost])

\tcolumn Amount
\t\tdataType: double
\t\tsummarizeBy: sum
\t\tsourceColumn: Amount

\tcolumn Cost
\t\tdataType: double
\t\tisHidden
\t\tsourceColumn: Cost

\tcolumn CustomerKey
\t\tdataType: int64
\t\tsourceColumn: CustomerKey

\tpartition Sales-partition = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Sql.Database("srv", "db")
\t\t\t\tin
\t\t\t\t    Source

\tannotation PBI_ResultType = Table
"""

CUSTOMER_TABLE = """table Customer
\tcolumn CustomerKey
\t\tdataType: int64
\t\tsourceColumn: CustomerKey

\tcolumn 'Company Name'
\t\tdataType: string
\t\tsourceColumn: CompanyName

\tpartition Customer-partition = m
\t\tmode: import
\t\tsource = let Source = 1 in Source
"""

CALC_GROUP_TABLE = """table 'Time Intelligence'
\tcalculationGroup
\t\tcalculationItem YTD = CALCULATE(SELECTEDMEASURE(), DATESYTD('Calendar'[Date]))

\t\tcalculationItem PY = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Calendar'[Date]))

\tcolumn 'Time Calculation'
\t\tdataType: string
\t\tsourceColumn: Name
"""

RELATIONSHIPS = """relationship rel_sales_customer
\tfromColumn: Sales.CustomerKey
\ttoColumn: Customer.CustomerKey

relationship rel_inactive
\tisActive: false
\tfromColumn: Sales.CustomerKey
\ttoColumn: Customer.CustomerKey
"""


def create_pbip_project(root: str) -> str:
    definition = os.path.join(root, "Demo.SemanticModel", "definition")
    tables = os.path.join(definition, "tables")
    os.makedirs(tables)
    for name, content in (
        ("Sales", SALES_TABLE),
        ("Customer", CUSTOMER_TABLE),
        ("Time Intelligence", CALC_GROUP_TABLE),
    ):
        with open(os.path.join(tables, f"{name}.tmdl"), "w", encoding="utf-8") as handle:
            handle.write(content)
    with open(os.path.join(definition, "relationships.tmdl"), "w", encoding="utf-8") as handle:
        handle.write(RELATIONSHIPS)
    with open(os.path.join(definition, "model.tmdl"), "w", encoding="utf-8") as handle:
        handle.write("model Model\n\tculture: en-US\n")
    with open(os.path.join(root, "Demo.pbip"), "w", encoding="utf-8") as handle:
        handle.write("{}")
    return root


def create_report_workspace(root: str) -> str:
    """Minimal PBIR workspace: one report, several pages, report- and page-level filters."""
    report = os.path.join(root, "Sales.Report")
    pages = os.path.join(report, "definition", "pages")

    def _write(path: str, data) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    _write(os.path.join(pages, "p1", "page.json"), {"displayName": "Overview"})
    _write(os.path.join(pages, "p1", "visuals", "v1", "visual.json"), {
        "visual": {
            "visualType": "card",
            "query": {"queryState": {"Values": {"projections": [{
                "field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}},
                                      "Property": "Total Sales"}},
                "queryRef": "Sales.Total Sales",
            }]}}},
        }
    })
    # PAGE filter in the current PBIR format: inside page.json, under
    # 'filterConfig'.
    _write(os.path.join(pages, "p2", "page.json"), {
        "displayName": "Detail",
        "filterConfig": {
            "filters": [{"field": {"Measure": {"Property": "Filter Only Measure"}}}]
        },
    })
    # Only a textual reference (queryRef), no binding: a "to verify" match.
    _write(os.path.join(pages, "p2", "visuals", "v2", "visual.json"), {
        "visual": {"visualType": "table"},
        "query": {"queryRef": "Sales.Margin %"},
    })
    # A page named like a measure: the search must stay restricted to
    # filterConfig and not produce a false positive.
    _write(os.path.join(pages, "p3", "page.json"), {"displayName": "Total Sales"})
    _write(os.path.join(pages, "p3", "visuals", "v3", "visual.json"), {
        "visual": {"visualType": "card"},
        "query": {"Measure": {"Property": "Other Measure"}},
    })
    # Column used directly by a visual: {"Column": ...} binding. No measure
    # reads it, so it used to look "unused".
    _write(os.path.join(pages, "p4", "page.json"), {"displayName": "Columns"})
    _write(os.path.join(pages, "p4", "visuals", "v4", "visual.json"), {
        "visual": {"visualType": "slicer"},
        "query": {"Column": {"Expression": {"SourceRef": {"Entity": "Customer"}},
                             "Property": "Company Name"}},
    })
    # Same idea, but through an alias of the 'From' clause and as a hierarchy level.
    _write(os.path.join(pages, "p5", "page.json"), {"displayName": "Hierarchy"})
    _write(os.path.join(pages, "p5", "visuals", "v5", "visual.json"), {
        "visual": {"visualType": "tableEx"},
        "query": {
            "From": [{"Name": "c", "Entity": "Customer", "Type": 0}],
            "Select": [
                {"HierarchyLevel": {
                    "Expression": {"Hierarchy": {
                        "Expression": {"SourceRef": {"Source": "c"}},
                        "Hierarchy": "Customer Hierarchy"}},
                    "Level": "CustomerKey"}},
            ],
        },
    })
    # Report-level filter in the current format (report.json/filterConfig).
    _write(os.path.join(report, "definition", "report.json"), {
        "filterConfig": {
            "filters": [{"field": {"Measure": {"Property": "Report Filter Measure"}}}]
        },
    })
    return root


def test_parser(project: str) -> None:
    print("\n[1] TMDL parser")
    _, lines = loader.read_table_lines(project, "Sales")

    table_block = tmdl.find_table_block(lines)
    check(table_block.name == "Sales", "table name read from the declaration")

    margin = tmdl.find_block(lines, "Margin %", "measure")
    check(margin.name == "Margin %", "quoted measure name with spaces and '%'")

    expression = tmdl.get_expression(lines, margin)
    check("DIVIDE(" in expression and "[Total Cost]" in expression,
          "multi-line expression reconstructed", expression)
    check("formatString" not in expression, "properties don't leak into the expression", expression)

    total = tmdl.find_block(lines, "Total Sales", "measure")
    check(tmdl.get_expression(lines, total) == "SUM(Sales[Amount])", "inline expression")

    cost_col = tmdl.find_block(lines, "Cost", "column")
    check(tmdl.is_hidden(lines, cost_col) is True, "isHidden shorthand recognized")
    check(tmdl.is_hidden(lines, table_block) is False,
          "table does NOT inherit isHidden from its columns")
    check(tmdl.is_hidden(lines, total) is False, "measure is not hidden")

    _, ti_lines = loader.read_table_lines(project, "Time Intelligence")
    item = tmdl.find_block(ti_lines, "YTD", "calculationItem")
    check(item.keyword == "calculationItem", "calculationItem found inside the calculationGroup")


def test_relationships_and_fact(project: str) -> None:
    print("\n[2] Relationships and fact tables")
    relationships = rels.parse_relationships(project)
    check(len(relationships) == 2, f"relationships found: {len(relationships)}")
    check(relationships[0]["from_table"] == "Sales" and relationships[0]["to_column"] == "CustomerKey",
          "qualified references resolved")
    check(relationships[1]["is_active"] is False, "inactive relationship recognized")
    check(rels.is_fact_table(project, "Sales") is True, "Sales is a fact table (many side)")
    check(rels.is_fact_table(project, "Customer") is False, "Customer is not a fact table")


def test_dax_lineage(project: str) -> None:
    print("\n[3] DAX lineage")
    report = lineage.build_dax_lineage(project)
    by_name = {element["name"]: element for element in report["elements"]}

    check("Margin %" in by_name, "quoted measure name present in the lineage", str(sorted(by_name)))
    margin = by_name.get("Margin %", {})
    check(set(margin.get("measures", [])) == {"Sales[Total Sales]", "Sales[Total Cost]"},
          "measure dependencies resolved with their full name", str(margin.get("measures")))
    check(not margin.get("columns"), "no false column dependencies", str(margin.get("columns")))

    total = by_name.get("Total Sales", {})
    check(total.get("columns") == ["Sales[Amount]"], "physical column resolved", str(total))

    # The fixture has no 'Calendar' table: the reference is reported as
    # unresolved instead of becoming a phantom column Calendar[Date].
    ytd = by_name.get("YTD", {})
    check(ytd.get("selected_measure") is True and not ytd.get("columns")
          and any("Calendar[Date]" in u and "table not found" in u for u in ytd.get("unresolved", [])),
          "calculation item analyzed, the missing table reported as unresolved", str(ytd))

    filtered = lineage.build_dax_lineage(project, "Margin %")
    check(len(filtered["elements"]) == 1, "filter by target measure")


def test_writing(project: str) -> None:
    print("\n[4] TMDL writing")

    result = writer_tools.set_display_folder(project, "Sales", "Total Sales", "measure", "Sales\\Base")
    _, lines = loader.read_table_lines(project, "Sales")
    block = tmdl.find_block(lines, "Total Sales", "measure")
    prop = tmdl.get_property(lines, block, "displayFolder")
    check(prop is not None, "displayFolder created", result)
    check(lines[prop.index].strip().startswith("displayFolder:"),
          "syntax uses ':' and not '='", lines[prop.index])
    check(lines[prop.index].startswith("\t\t"), "indented at property level", repr(lines[prop.index]))

    repeated = writer_tools.set_display_folder(project, "Sales", "Total Sales", "measure", "Sales\\Base")
    check("No changes" in repeated, "idempotent: no duplicate on second run", repeated)

    writer_tools.set_item_visibility(project, "Sales", "Total Cost", "measure", True)
    _, lines = loader.read_table_lines(project, "Sales")
    block = tmdl.find_block(lines, "Total Cost", "measure")
    check(tmdl.is_hidden(lines, block) is True, "isHidden written and readable")

    # A calculation item has no formatString in TOM: its format is the DAX
    # expression 'formatStringDefinition'.
    result = writer_tools.format_measure(project, "Time Intelligence", "YTD", "#,##0.0")
    _, lines = loader.read_table_lines(project, "Time Intelligence")
    item = tmdl.find_block(lines, "YTD", "calculationItem")
    item_text = "".join(lines[item.start:item.end])
    check('formatStringDefinition = "#,##0.0"' in item_text
          and tmdl.get_property(lines, item, "formatString") is None,
          "calculationItem fallback writes formatStringDefinition", result + "\n" + item_text)

    result = writer_tools.create_measure(
        project, "Sales", "Sales LY",
        "CALCULATE(\n    [Total Sales],\n    SAMEPERIODLASTYEAR('Calendar'[Date])\n)",
        display_folder="KPI", format_string="#,##0", description="Sales for the previous year",
    )
    _, lines = loader.read_table_lines(project, "Sales")
    new_measure = tmdl.find_block(lines, "Sales LY", "measure")
    check(new_measure.indent == "\t", "new measure indented inside the table block",
          repr(lines[new_measure.start]))
    check("SAMEPERIODLASTYEAR" in tmdl.get_expression(lines, new_measure), "multi-line expression written")
    check(tmdl.get_doc_comments(lines, new_measure), "description written as a /// comment")

    partition = tmdl.find_block(lines, "Sales-partition", "partition")
    check(new_measure.start < partition.start, "inserted before the partition, not at end of file")
    table = tmdl.find_table_block(lines)
    check(new_measure.start < table.end, "new measure within the table block boundaries")

    try:
        writer_tools.create_measure(project, "Sales", "Sales LY", "1")
        check(False, "duplicate creation rejected")
    except Exception as exc:  # noqa: BLE001
        check("already exists" in str(exc), "duplicate creation rejected", str(exc))

    writer_tools.create_calculation_item(project, "Time Intelligence", "QTD", "SELECTEDMEASURE()")
    _, lines = loader.read_table_lines(project, "Time Intelligence")
    qtd = tmdl.find_block(lines, "QTD", "calculationItem")
    check(qtd.indent == "\t\t", "calculationItem nested in the calculationGroup", repr(lines[qtd.start]))


def test_autodoc(project: str) -> None:
    print("\n[5] Autodoc")
    preview = reader_tools.get_smart_description_preview(project, "Sales", "Margin %", "Margin percentage")
    check("Proposed description" in preview, "preview generated", preview)
    check("Total Sales" in preview, "preview mentions the real dependencies", preview)
    check("NOT written" in preview, "the preview says the technical context is not written", preview)

    first_look = reader_tools.get_smart_description_preview(project, "Sales", "Margin %")
    check("Total Sales" in first_look and "base_description" in first_look,
          "preview without a description: technical context only", first_look)

    writer_tools.write_smart_description(project, "Sales", "Margin %", "Margin percentage")
    _, lines = loader.read_table_lines(project, "Sales")
    block = tmdl.find_block(lines, "Margin %", "measure")
    comments = tmdl.get_doc_comments(lines, block)
    check([c.strip() for c in comments] == ["/// Margin percentage"],
          "ONLY the business description is written above the declaration", str(comments))
    check(all(c.strip().startswith("///") for c in comments), "comment format is correct")

    repeated = writer_tools.write_smart_description(project, "Sales", "Margin %", "Margin percentage")
    check("No changes" in repeated, "autodoc is idempotent", repeated)

    try:
        writer_tools.write_smart_description(project, "Sales", "Total Cost", "")
        check(False, "an empty description is rejected")
    except ValueError as exc:
        check("base_description" in str(exc), "an empty description is rejected", str(exc))

    try:
        writer_tools.write_smart_description(project, "Sales", "Amount", "Sale amount.", element_type="column")
        check(False, "fact table columns are protected")
    except Exception as exc:  # noqa: BLE001
        check("fact table" in str(exc), "fact table columns are protected", str(exc))

    dimension = writer_tools.write_smart_description(
        project, "Customer", "Company Name", "Legal name of the customer.", element_type="column")
    check("Smart-Doc" in dimension, "dimension column can be documented", dimension)


def test_ols_roles(project: str) -> None:
    print("\n[6] Object-Level Security (OLS) of the roles")

    check(reader_tools.get_ols_permissions(project) == "No security roles defined in the model.",
          "no roles -> explicit message")

    # Role creation + whole table hidden (whole-table OLS).
    result = writer_tools.set_ols_permission(project, "Analysts", table_name="Customer", hidden=True)
    check("hidden" in result.lower(), "table hidden on the first run", result)

    state = ols_roles.get_ols_state(project, "Analysts")
    role = state["roles"][0]
    check(role["model_permission"] == "read", "role created with default modelPermission 'read'", str(role))
    check(role["tables"][0]["table"] == "Customer" and role["tables"][0]["hidden_table"] is True,
          "Customer table marked as hidden", str(role))

    repeated = writer_tools.set_ols_permission(project, "Analysts", table_name="Customer", hidden=True)
    check("No changes" in repeated, "idempotent: table already hidden", repeated)

    # Hidden column on another table of the same role.
    result = writer_tools.set_ols_permission(
        project, "Analysts", table_name="Sales", column_name="Cost", hidden=True,
    )
    check("Cost" in result and "hidden" in result.lower(), "column hidden", result)

    state = ols_roles.get_ols_state(project, "Analysts")
    tables_by_name = {t["table"]: t for t in state["roles"][0]["tables"]}
    check(tables_by_name["Sales"]["hidden_columns"] == ["Cost"],
          "hidden column detected when reading", str(tables_by_name["Sales"]))
    check(tables_by_name["Sales"]["hidden_table"] is False,
          "the 'Sales' table stays visible: only the column is hidden", str(tables_by_name["Sales"]))

    report = reader_tools.get_ols_permissions(project, "Analysts")
    check("Analysts" in report and "Customer" in report and "Cost" in report,
          "text report includes role, hidden table and hidden column", report)

    # Column that doesn't exist in the model -> error.
    try:
        writer_tools.set_ols_permission(project, "Analysts", table_name="Sales", column_name="DoesNotExist")
        check(False, "nonexistent column rejected")
    except Exception as exc:  # noqa: BLE001
        check("no declaration found" in str(exc).lower(), "nonexistent column rejected", str(exc))

    # Making the column visible again: both the columnPermission block and the
    # (now empty) tablePermission are removed, without touching 'Customer'.
    result = writer_tools.set_ols_permission(
        project, "Analysts", table_name="Sales", column_name="Cost", hidden=False,
    )
    check("visible" in result.lower(), "column made visible", result)

    state = ols_roles.get_ols_state(project, "Analysts")
    tables_by_name = {t["table"]: t for t in state["roles"][0]["tables"]}
    check("Sales" not in tables_by_name,
          "tablePermission on 'Sales' removed because it was left empty", str(tables_by_name))
    check(tables_by_name["Customer"]["hidden_table"] is True,
          "restoring 'Sales' didn't touch 'Customer'", str(tables_by_name))

    repeated = writer_tools.set_ols_permission(
        project, "Analysts", table_name="Sales", column_name="Cost", hidden=False,
    )
    check("No changes" in repeated, "idempotent: column already visible", repeated)

    # Missing role and create_role_if_missing=False -> explicit error.
    try:
        writer_tools.set_ols_permission(
            project, "MissingRole", table_name="Customer", create_role_if_missing=False,
        )
        check(False, "missing role without auto-creation -> error")
    except Exception as exc:  # noqa: BLE001
        check("not found" in str(exc).lower(), "missing role without auto-creation -> error", str(exc))

    # Pure role (modelPermission only), without touching tables.
    writer_tools.set_ols_permission(project, "ReadOnly", model_permission="read")
    check("ReadOnly" in ols_roles.list_role_names(project), "role created without table restrictions")


def test_reading(project: str) -> None:
    print("\n[7] Read tools")
    tables = reader_tools.list_tables(project)
    names = [line for line in tables.splitlines() if line and not line.startswith("===")]
    check(isinstance(tables, str), "list_tables returns text (MCP schema)", type(tables).__name__)
    check(names == ["Customer", "Sales", "Time Intelligence"], "list_tables", str(tables))

    measure = reader_tools.get_dax_measure(project, "Sales", "Margin %")
    check("DIVIDE(" in measure, "get_dax_measure with a .pbip-based path", measure)

    relationships = reader_tools.get_relationships(project, "Customer")
    check("Sales[CustomerKey]" in relationships and "inactive" in relationships,
          "compact get_relationships", relationships)


def test_reports(workspace: str) -> None:
    print("\n[8] PBIR report usage")

    result = find_measure_usage_in_reports(workspace, measure_names=["Total Sales", "Total Cost"])
    check("'Total Sales': 1 usage" in result, "measure usage found in visual", result)
    check("Overview" in result, "page displayName used as label", result)
    check("'Total Cost': NO usages found" in result, "unused measure reported", result)

    # Page filters of current PBIR live in page.json under 'filterConfig'.
    # Previously only 'filters.json' was searched, which no longer exists.
    page_filter = find_measure_usage_in_reports(workspace, "Filter Only Measure")
    check("page-level filter" in page_filter,
          "measure used only in a page filter (page.json/filterConfig)", page_filter)
    check("Detail" in page_filter, "the page filter reports the page name", page_filter)

    report_filter = find_measure_usage_in_reports(workspace, "Report Filter Measure")
    check("report-level filter" in report_filter,
          "measure used only in a report filter (report.json/filterConfig)", report_filter)

    # Columns: a report can use them without any measure reading them.
    column = find_measure_usage_in_reports(workspace, column_names=["Customer[Company Name]"])
    check("Columns" in column, "column used by a visual (Column binding)", column)

    level = find_measure_usage_in_reports(workspace, column_names=["Customer[CustomerKey]"])
    check("Hierarchy" in level, "column as a hierarchy level, with the 'From' alias resolved", level)

    other = find_measure_usage_in_reports(workspace, column_names=["Sales[CustomerKey]"])
    check("no usages" in other.lower(),
          "a namesake column of another table doesn't count as a usage", other)

    # ...and the search stays restricted to filterConfig: a page named like a
    # measure must not count as a usage.
    namesake = find_measure_usage_in_reports(workspace, "Total Sales")
    check("Overview" in namesake, "the measure is found in the visual that uses it", namesake)
    check("p3" not in namesake and "| Total Sales |" not in namesake,
          "a page displayName equal to the measure name is not a false positive", namesake)


def _realistic_payload(n_columns: int = 60) -> str:
    """Payload similar to the official server's: internal metadata everywhere."""
    columns = [
        {
            "name": f"Column_{i}",
            "dataType": "int64" if i % 3 else "string",
            "isHidden": i % 10 == 0,
            "summarizeBy": "none",
            "sourceColumn": f"COL_{i}",
            "lineageTag": f"{i:08d}-aaaa-bbbb-cccc-{i:012d}",
            "changedProperties": [{"property": "IsHidden"}],
            "modifiedTime": "2026-01-01T00:00:00Z",
            "annotations": [{"name": "SummarizationSetBy", "value": "Automatic"}],
        }
        for i in range(n_columns)
    ]
    return json.dumps({"table": {"name": "Sales", "columns": columns}})


def test_output_shaping() -> None:
    print("\n[9] Bridge output reduction (output_shaping)")
    import re as _re

    from mcp_data_platform_toolkit.mcp_common import output_shaping as shaping

    payload = _realistic_payload()
    full_size = len(payload)

    # 1. Noise removal: no loss of useful information.
    no_noise = shaping._transform(json.loads(payload), shaping.ShapeConfig(drop_noise=True))
    columns = no_noise["table"]["columns"]
    check(len(columns) == 60, "all elements kept after noise removal")
    check(all("lineageTag" not in c and "annotations" not in c for c in columns),
          "internal metadata removed")
    check(all(c["name"] and c["dataType"] and c["sourceColumn"] for c in columns),
          "informative fields all intact")
    saving = 1 - len(json.dumps(no_noise, separators=(",", ":"))) / full_size
    check(saving > 0.5, f"lossless saving: {saving:.0%}")

    # 2. Factoring: lossless, reconstructible.
    factored = shaping._transform(
        json.loads(payload),
        shaping.ShapeConfig(drop_noise=True, drop_empty=True, factor_shared=True),
    )
    node = factored["table"]["columns"]
    check(isinstance(node, dict) and "_shared" in node, "shared fields factored out", str(type(node)))
    check(node["_shared"].get("summarizeBy") == "none", "shared value declared only once")
    rebuilt = [{**node["_shared"], **item} for item in node["items"]]
    check(len(rebuilt) == 60 and rebuilt[7]["name"] == "Column_7",
          "factoring is reversible without loss")

    # 3. Profile: all identifiers are kept.
    profiled = shaping._transform(
        json.loads(payload),
        shaping.ShapeConfig(drop_noise=True, drop_empty=True, profile_lists=True, sample_items=1),
    )
    profile = profiled["table"]["columns"]["_profile"]
    check(profile["count"] == 60, "real count in the profile")
    check(len(profile.get("name", [])) == 60,
          "ALL column names preserved in the profile", str(len(profile.get("name", []))))
    check(profile["fields"]["dataType"]["type"] == "string", "per-field profile present")

    # 4. The reduction ladder must never make the output grow.
    sizes = [
        len(json.dumps(shaping._transform(json.loads(payload), cfg), separators=(",", ":"), default=str))
        for _label, cfg in shaping.REDUCTION_LADDER
    ]
    check(sizes == sorted(sizes, reverse=True), "monotonic reduction ladder", str(sizes))

    # 5. Generous budget -> stops at the least destructive level that fits.
    result = shaping.shape_output(payload, max_chars=3000, tool_name="table_operations")
    check("lineageTag" not in result, "under a tight budget noise goes first")
    check("Column_59" in result, "last element survives (no positional cut)")

    # 6. Diagnostic fields are never compressed.
    with_error = json.dumps({
        "error": {"message": "Unable to connect: " + "detail " * 200, "code": "TMDL_0042"},
        "columns": [{"name": f"C{i}", "lineageTag": "x" * 40} for i in range(100)],
    })
    reduced = shaping.shape_output(with_error, max_chars=600, tool_name="t")
    check("TMDL_0042" in reduced, "error code preserved under a tight budget")
    check(reduced.count("detail") > 100, "error message not truncated", str(reduced.count("detail")))

    # 7. Deferred retrieval: nothing is truly lost.
    result = shaping.shape_output(payload, max_chars=800, tool_name="table_operations")
    found = _re.search(r"result_id='([0-9a-f]+)'", result)
    check(found is not None, "retrieval id reported in the note", result[-200:])
    if found:
        entry = shaping.get_result(found.group(1))
        check(len(entry["data"]["table"]["columns"]) == 60, "full payload kept in cache")
        portion = shaping.resolve_path(entry["data"], "table.columns[3]")
        check(portion["lineageTag"].startswith("00000003"),
              "discarded metadata still retrievable via path")
        try:
            shaping.resolve_path(entry["data"], "table.missing")
            check(False, "wrong path reported with available keys")
        except KeyError as exc:
            check("columns" in str(exc), "wrong path reported with available keys", str(exc))

    # 8. Non-JSON text: cut at a sentence boundary, not mid-word.
    text = ". ".join(f"Sentence number {i} of the log" for i in range(200))
    cut = shaping.shape_output(text, max_chars=300, tool_name="t")
    prefix = cut.split(" [truncated")[0].rstrip().rstrip(".")
    check(prefix.endswith("of the log"), "free text cut at a sentence boundary, not mid-word",
          prefix[-40:])


def test_guidelines() -> None:
    print("\n[10] Description guidelines")
    import asyncio

    from mcp.server.mcpserver import MCPServer

    from mcp_data_platform_toolkit.semantic_model.description_guidelines import (
        SHORT_GUIDELINES,
        load_full_guidelines,
        with_short_guidelines,
    )

    text = load_full_guidelines()
    check(bool(text.strip()), "full guidelines not empty")
    check("FORBIDDEN" in text and "EXAMPLES" in text, "key sections present")
    check("\n\n\n" not in text, "extra blank lines removed")
    check(not any(line != line.rstrip() for line in text.splitlines()),
          "no trailing spaces")

    double = with_short_guidelines(with_short_guidelines("Docstring."))
    check(double.count(SHORT_GUIDELINES) == 1, "helper is idempotent: guidelines only once")

    mcp = MCPServer("selftest")
    reader_tools.register_reader_tools(mcp)
    writer_tools.register_writer_tools(mcp)
    descriptions = {t.name: (t.description or "") for t in asyncio.run(mcp.list_tools())}

    for name in ("write_smart_description", "get_smart_description_preview"):
        check(descriptions.get(name, "").count(SHORT_GUIDELINES) == 1,
              f"short version present exactly once in '{name}'")
        check("Args:" in descriptions.get(name, ""),
              f"original docstring kept in '{name}'")

    check("get_description_guidelines" in descriptions,
          "get_description_guidelines registered among the read tools")
    check(sum(1 for d in descriptions.values() if SHORT_GUIDELINES in d) == 2,
          "no other tool inherits the guidelines")

    read_only = MCPServer("selftest-ro")
    reader_tools.register_reader_tools(read_only)
    ro_names = {t.name for t in asyncio.run(read_only.list_tools())}
    check("get_description_guidelines" in ro_names,
          "get_description_guidelines available in read-only mode")
    check("write_smart_description" not in ro_names,
          "write tools stay out of read-only mode")


def test_ambiguous_model(root: str) -> None:
    """Several models under the same path: the tool must ASK, not fail."""
    print("\n[11] Path containing several semantic models")
    from mcp_data_platform_toolkit.mcp_common.errors import tool_error_handler

    workspace = os.path.join(root, "workspace_multi")
    for name in ("Alpha", "Beta"):
        tables = os.path.join(workspace, name, f"{name}.SemanticModel", "definition", "tables")
        os.makedirs(tables, exist_ok=True)
        with open(os.path.join(tables, "T.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("table T\n\tcolumn C\n\t\tdataType: string\n")

    tool = tool_error_handler(reader_tools.list_tables)
    try:
        response = tool(workspace)
        raised = ""
    except Exception as exc:  # noqa: BLE001 - it is what is being checked
        response = ""
        raised = type(exc).__name__

    check(not raised, "ambiguous path: no error raised", raised)
    check(isinstance(response, str) and "Which one does the request refer to" in response,
          "ambiguous path: the response asks the question", str(response)[:120])
    check("Alpha.SemanticModel" in str(response) and "Beta.SemanticModel" in str(response),
          "ambiguous path: lists the models to choose from", str(response)[:120])


def test_line_endings(project: str) -> None:
    """A one-line edit must stay a one-line edit on disk."""
    print("\n[12] Line endings of TMDL files")
    path = loader.get_table_file_path(project, "Sales")
    cr, lf = chr(13), chr(10)

    def count(file_path: str) -> tuple:
        text = io.open(file_path, encoding="utf-8", newline="").read()
        crlf = text.count(cr + lf)
        return crlf, text.count(lf) - crlf

    for label, terminator in (("CRLF", cr + lf), ("LF", lf)):
        text = io.open(path, encoding="utf-8").read()
        io.open(path, "w", encoding="utf-8", newline=terminator).write(text)
        before = count(path)

        folder = f"Folder {label}"
        writer_tools.set_display_folder(
            project, "Sales", "Total Sales", element_type="measure",
            display_folder=folder)
        after = count(path)
        content = io.open(path, encoding="utf-8").read()

        # The write really happened...
        check(folder in content,
              f"write on {label} file: the change was applied",
              content[:80])
        # ...and the file neither changed nor mixed its terminators.
        expect_crlf = label == "CRLF"
        ok = (after[1] == 0 and after[0] > 0) if expect_crlf else (after[0] == 0 and after[1] > 0)
        check(ok, f"write on {label} file: terminators preserved",
              f"before=(CRLF={before[0]}, LF={before[1]}) after=(CRLF={after[0]}, LF={after[1]})")

    # A new file is born consistent with the nearby .tmdl files, not with
    # Python's default. The model is first made CRLF everywhere, as Power BI
    # Desktop writes it: the fixture files are written with the platform's
    # line ending, so on Linux/macOS the 'definition' folder next to the new
    # file was LF and the check failed there while passing on Windows.
    definition = paths.find_definition_folder(project)
    for folder, _dirs, files in os.walk(definition):
        for name in files:
            if name.lower().endswith(".tmdl"):
                model_file = os.path.join(folder, name)
                with open(model_file, encoding="utf-8") as handle:
                    text = handle.read()
                with open(model_file, "w", encoding="utf-8", newline=cr + lf) as handle:
                    handle.write(text)
    roles = os.path.join(os.path.dirname(path), "..", "roles.tmdl")
    if os.path.exists(roles):
        os.remove(roles)
    ols_roles.set_ols_permission(project, "Line Ending Role", model_permission="read")
    if os.path.exists(roles):
        crlf, lf_only = count(roles)
        check(lf_only == 0 and crlf > 0,
              "new roles file: line endings consistent with the model",
              f"CRLF={crlf} LF={lf_only}")


def test_mcp_contract(project: str, root: str) -> None:
    """
    Calls through the REAL MCP layer, not directly on the functions.

    All the other tests invoke the Python functions: that way they don't
    notice the validation MCPServer applies to the return value based on the
    type annotation. That is where the `list_tables` bug was hiding, when it
    was annotated `list[str]`: the `AmbiguousModelError` response (a textual
    question) failed the schema and the client received an opaque 'Error
    executing tool list_tables' instead of the question. This test takes the
    same path as a real client.
    """
    print("\n[13] MCP contract (tools' input/output schema)")
    import asyncio

    original_argv = list(sys.argv)
    sys.argv = ["server.py", "--access=full_access", "--msft-bridge=off"]
    try:
        from mcp_data_platform_toolkit import server as server_module
        mcp = server_module.build_server()
    finally:
        sys.argv = original_argv

    async def call(name: str, arguments: dict):
        return await mcp.call_tool(name, arguments)

    def text_of(result) -> str:
        blocks = getattr(result, "content", None)
        if blocks is None:
            blocks = result[0] if isinstance(result, tuple) else result
        return " ".join(getattr(b, "text", "") for b in blocks)

    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    check("list_tables" in tools and "create_measure" in tools,
          "full access: read and write tools are registered")

    try:
        response = text_of(asyncio.run(call("list_tables", {"project_path": project})))
        error = ""
    except Exception as exc:  # noqa: BLE001 - it is what is being checked
        response, error = "", f"{type(exc).__name__}: {exc}"
    check(not error and "Sales" in response,
          "list_tables via MCP: valid response", error or response[:120])

    # Ambiguous path through the MCP layer: the QUESTION must arrive, not a
    # schema validation error.
    workspace = os.path.join(root, "workspace_mcp")
    for name in ("Alpha", "Beta"):
        folder = os.path.join(workspace, name, f"{name}.SemanticModel", "definition", "tables")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "T.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("table T\n\tcolumn C\n\t\tdataType: string\n")

    try:
        response = text_of(asyncio.run(call("list_tables", {"project_path": workspace})))
        error = ""
    except Exception as exc:  # noqa: BLE001
        response, error = "", f"{type(exc).__name__}: {exc}"
    check(not error, "ambiguous path via MCP: no protocol error", error)
    check("Which one does the request refer to" in response,
          "ambiguous path via MCP: the question arrives, not an opaque error", response[:160])

    # A real failure must stay a failure (isError), not a string.
    try:
        result = asyncio.run(call("list_tables", {"project_path": os.path.join(root, "missing")}))
        failed = bool(getattr(result, "is_error", False))
    except Exception:  # noqa: BLE001 - an exception is a correct failure too
        result, failed = None, True
    check(failed, "missing path via MCP: the tool really fails (isError)",
          str(result)[:160])


def test_atomic_write(project: str) -> None:
    """An error halfway through a write must not leave a truncated TMDL on disk."""
    print("\n[14] Atomic TMDL writes")
    path = loader.get_table_file_path(project, "Sales")
    original = io.open(path, encoding="utf-8").read()

    lines = tmdl_io.read_tmdl_lines(path)

    def failing_lines():
        yield from lines[:2]
        raise OSError("simulated disk full")

    try:
        tmdl_io.write_tmdl_lines(path, failing_lines())
        failed = False
    except OSError:
        failed = True

    check(failed, "the interrupted write propagates the error")
    check(io.open(path, encoding="utf-8").read() == original,
          "the original file is intact after the interrupted write")

    leftovers = [n for n in os.listdir(os.path.dirname(path)) if n.startswith(".tmp_")]
    check(not leftovers, "no temporary file left on disk", str(leftovers))

    # And the normal write keeps working.
    tmdl_io.write_tmdl_lines(path, lines)
    check(io.open(path, encoding="utf-8").read() == original,
          "a full rewrite reproduces the identical file")


def test_lossless_reduction(root: str) -> None:
    """
    On a large model the response must shrink WITHOUT elements disappearing.

    The risk this test guards against: `shape_lines` truncates the tail, and
    on a list sorted by table it made 91% of the measures of a 1,200-element
    model disappear. A partial answer that looks complete leads to the
    conclusion "this measure depends on nothing" when the line was simply past
    the cut.
    """
    print("\n[15] Progressive reduction without losing elements")
    from mcp_data_platform_toolkit.semantic_model.dax_lineage import build_dax_lineage, format_dax_lineage

    project = os.path.join(root, "large")
    tables = os.path.join(project, "Large.SemanticModel", "definition", "tables")
    os.makedirs(tables, exist_ok=True)
    for t in range(40):
        lines = [f"table Table{t:02d}\n"]
        for m in range(10):
            lines.append(
                f"\tmeasure 'Measure {t:02d}_{m:02d}' = "
                f"SUM(Table{t:02d}[Col{m:02d}]) + [Measure {t:02d}_{(m + 1) % 10:02d}]\n"
            )
        for c in range(10):
            lines.append(f"\tcolumn Col{c:02d}\n\t\tdataType: double\n")
        with open(os.path.join(tables, f"Table{t:02d}.tmdl"), "w", encoding="utf-8") as handle:
            handle.writelines(lines)
    # A measure that references nothing: it has no lineage, but it must not
    # disappear from the output.
    with open(os.path.join(tables, "Separators.tmdl"), "w", encoding="utf-8") as handle:
        handle.write("table Separators\n\tmeasure 'Separator Measure' = BLANK()\n")

    report = build_dax_lineage(project)
    total = len(report["elements"])
    check(total >= 400, f"test model large enough ({total} elements)")
    check([e["name"] for e in report["no_dependencies"]] == ["Separator Measure"],
          "a measure without dependencies is listed separately", str(report["no_dependencies"]))

    def named(text: str) -> int:
        found = 0
        for element in report["elements"]:
            name, table = element["name"], element["table"]
            if f"{table}[{name}]" in text:
                found += 1
                continue
            for line in text.splitlines():                      # inventory form
                if line.startswith(f"{table} (") and name in line.split(": ", 1)[-1].split(", "):
                    found += 1
                    break
        return found

    full = format_dax_lineage(report)
    check(named(full) == total, "full rendering: all elements present")

    # The limit must be respected at ANY budget, reduction note included:
    # counting the note only after the check made exactly the responses
    # reduced to fit the budget exceed it. The only allowed exception is the
    # last-resort message ("not even the list of names fits"), which is not
    # truncated: below that length there is no useful answer to give.
    floor = len(format_dax_lineage(report, max_chars=1))
    overruns = [
        (budget, len(format_dax_lineage(report, max_chars=budget)))
        for budget in range(len(full) + 100, 50, -13)
        if len(format_dax_lineage(report, max_chars=budget)) > max(budget, floor)
    ]
    check(not overruns, "the budget is respected at every rung of the ladder", str(overruns[:3]))
    check(floor < 500 and "target_measure" in format_dax_lineage(report, max_chars=1),
          f"the last-resort message stays short and useful ({floor} characters)")

    # Tighter and tighter budgets: as long as the inventory fits, no element
    # may disappear, however much the detail is reduced.
    for budget in (40000, 20000, 15000):
        reduced = format_dax_lineage(report, max_chars=budget)
        check(len(reduced) <= budget,
              f"budget {budget}: the response respects the limit", f"{len(reduced)} characters")
        check(named(reduced) == total,
              f"budget {budget}: no element lost ({total} expected)",
              f"found {named(reduced)}")
        check("Separator Measure" in reduced,
              f"budget {budget}: the element without dependencies is still named")

    # Below the inventory level the elements no longer fit the budget: that
    # is fine, as long as the response SAYS so instead of looking complete.
    minimum = format_dax_lineage(report, max_chars=1200)
    check(named(minimum) < total, "minimum budget: the detail is given up")
    check("target_measure" in minimum,
          "minimum budget: the response explains how to get the detail", minimum[:200])


def test_complete_usage_summary(workspace: str) -> None:
    """The usage summary always covers ALL the searched fields."""
    print("\n[16] Complete usage summary for every field")
    from mcp_data_platform_toolkit.reports.report_tools import _format_usages, collect_field_usage

    names = ["Total Sales", "Missing Measure", "Margin %"]
    report = collect_field_usage(workspace, [{"kind": "measure", "name": n} for n in names])

    full = _format_usages(report, max_chars=None)
    check("SUMMARY" in full, "the summary always precedes the detail", full[:120])

    # Real case where the detail is lost: many fields, each used in dozens of
    # places. The report is built by hand to keep the test deterministic: with
    # the test workspace the output always fits the budget and the reduction
    # branch would never be exercised.
    many = {
        "fields": [f"Measure {i:03d}" for i in range(30)],
        "usages": {
            f"Measure {i:03d}": [
                {"report": "R", "page": f"Page {p}", "kind": "visual",
                 "label": f"R | Page {p} | visual 'v{p}' (type: table)"}
                for p in range(i % 5)          # some measures stay without usages
            ]
            for i in range(30)
        },
        "unreadable": [],
    }

    full = _format_usages(many, max_chars=None)
    reduced = _format_usages(many, max_chars=2000)
    check(len(full) > 2000, "the test case really exceeds the budget",
          f"{len(full)} characters")
    check(len(reduced) <= 2000, "budget 2000: the response respects the limit",
          f"{len(reduced)} characters")
    check(all(f"Measure {i:03d}" in reduced for i in range(30)),
          "budget 2000: ALL 30 searched fields appear in the summary", reduced[:250])
    check("NO usages" in reduced,
          "budget 2000: fields without usages stay distinguishable", reduced[:250])
    check("COMPLETE" in reduced,
          "budget 2000: the response declares the counts complete", reduced[-250:])
    check("visual 'v" not in reduced,
          "budget 2000: what drops is the detail, not the summary", reduced[-250:])


# ---------------------------------------------------------------------------
# Regressions of the security and robustness review
# ---------------------------------------------------------------------------
# Every test below guards a defect that was REPRODUCED on earlier code, not a
# theoretical risk. The comment of each one says what used to happen, because
# that is the reason the test exists.


def test_regression_corruption_and_injection(project: str) -> None:
    """
    Two ways of producing a TMDL file that Power BI Desktop can't reopen.

    1. File without a trailing newline: `set_property` appended the new line
       to the previous one, merging two properties into one. The tool reported
       success because the write itself had succeeded.
    2. Value or name with a line break: TMDL has no escape for newlines, so
       the value was written across two lines and the second came back as a
       property of its own ('displayFolder: X\\nisHidden: true').
    """
    print("\n[17] TMDL corruption and injection through values")

    lines = ["table T\n", "\tmeasure M = 1\n", "\t\tformatString: 0"]  # no trailing '\n'
    block = tmdl.find_block(lines, "M", "measure")
    new_lines, changed = tmdl.set_property(lines, block, "displayFolder", "Folder")
    text = "".join(new_lines)
    check(changed, "the property is actually added")
    check("formatString: 0\n" in text,
          "the previous line without a newline gets terminated", repr(text))
    check("0\t\tdisplayFolder" not in text,
          "no two properties merged on the same line", repr(text))
    check(len(tmdl.find_all_blocks(new_lines)) == len(tmdl.find_all_blocks(lines)),
          "the file stays parseable after the insertion")

    for value in ("Folder\nisHidden: true", "Folder\rX"):
        try:
            tmdl.format_text_value(value)
            rejected = False
        except tmdl.TmdlError:
            rejected = True
        check(rejected, f"value with a line break rejected ({value!r})")

    try:
        tmdl.quote_object_name("Name\nmeasure Evil = 1")
        rejected = False
    except tmdl.TmdlError:
        rejected = True
    check(rejected, "element name with a line break rejected")

    check(tmdl.format_text_value("Folder\\Sub") == "Folder\\Sub",
          "a legitimate nested displayFolder is still accepted")

    # Same defense seen from the tool, not just the parser: the file must not be touched.
    path = loader.get_table_file_path(project, "Sales")
    before = io.open(path, encoding="utf-8").read()
    try:
        writer_tools.set_display_folder(
            project, "Sales", "Total Sales", display_folder="X\nisHidden: true")
        rejected = False
    except tmdl.TmdlError:
        rejected = True
    check(rejected, "set_display_folder rejects a value with a line break")
    check(io.open(path, encoding="utf-8").read() == before,
          "the file was not modified by the injection attempt")


def test_regression_paths(project: str) -> None:
    """
    Table and role names come from a language model and ended up in
    `os.path.join` unchecked: '../../../../x' escaped the model folder and was
    read or REWRITTEN. For roles it was even worse, because
    `write_tmdl_lines` calls `os.makedirs`: not an overwrite, an arbitrary
    write that also creates the folder tree.
    """
    print("\n[18] Path confinement")

    for name in ("../../../../secret", "..\\..\\secret", "sub/dir", "C:evil", ".."):
        try:
            loader.get_table_file_path(project, name)
            rejected = False
        except (paths.PathNotAllowedError, ValueError):
            rejected = True
        except FileNotFoundError:
            rejected = True  # nonexistent simple name: it doesn't escape anyway
        check(rejected, f"out-of-bounds table name rejected ({name!r})")

    check(os.path.isfile(loader.get_table_file_path(project, "Sales")),
          "a legitimate table is still resolvable")

    # 'definition/roles/<name>.tmdl' layout: the one where the role name
    # becomes a file name.
    roles_folder = os.path.join(paths.find_definition_folder(project), "roles")
    os.makedirs(roles_folder, exist_ok=True)
    outside = os.path.join(os.path.dirname(project), "pwn.tmdl")
    try:
        ols_roles.set_ols_permission(project, "../../../../../pwn")
        rejected = False
    except (paths.PathNotAllowedError, ValueError):
        rejected = True
    check(rejected, "out-of-bounds role name rejected")
    check(not os.path.exists(outside), "no file created outside the project", outside)
    shutil.rmtree(roles_folder, ignore_errors=True)


def test_regression_concurrent_write(project: str) -> None:
    """
    The read-all / modify / rewrite-all pattern had no protection: whoever
    wrote last erased the other's change WITHOUT an error. `os.replace`
    guarantees the file is never truncated, not that nobody lost their work.
    Autodoc is the worst case, because it freezes lines and block indices
    well before writing.
    """
    print("\n[19] Writing to a file changed after it was read")

    path = loader.get_table_file_path(project, "Sales")
    fingerprint = tmdl_io.file_fingerprint(path)
    lines = tmdl_io.read_tmdl_lines(path)

    time.sleep(0.02)
    with open(path, "a", encoding="utf-8") as handle:  # "someone else's" write
        handle.write("\n")
    after_other = io.open(path, encoding="utf-8").read()

    try:
        tmdl_io.write_tmdl_lines(path, lines, expected_fingerprint=fingerprint)
        refused = False
    except tmdl.TmdlError:
        refused = True
    check(refused, "writing to an already changed file is refused")
    check(io.open(path, encoding="utf-8").read() == after_other,
          "the other party's change was NOT overwritten")

    # With an updated fingerprint the normal write works as before.
    updated = tmdl_io.file_fingerprint(path)
    tmdl_io.write_tmdl_lines(path, tmdl_io.read_tmdl_lines(path), expected_fingerprint=updated)
    check(True, "writing with a valid fingerprint keeps working")

    # Same protection through the autodoc tool.
    state = autodoc.gather_autodoc_state(
        project, "Sales", "Total Sales", "measure", "Unique test description.")
    time.sleep(0.02)
    with open(state["file_path"], "a", encoding="utf-8") as handle:
        handle.write("\n")
    try:
        writer_tools.apply_autodoc_changes(state, "append")
        refused = False
    except tmdl.TmdlError:
        refused = True
    check(refused, "autodoc refuses to apply a stale state")


def test_regression_degenerate_input(root: str) -> None:
    """
    Inputs that used to break much more than themselves.

    - UTF-8 BOM: read with plain 'utf-8' it stayed attached to the first
      character, the `table` block became invisible and `load_semantic_model`
      SILENTLY fell back to the file name, mislabeling every lineage edge.
    - Deeply nested JSON: `RecursionError` escaped the per-file handling and
      took down the whole workspace scan.
    - Valid JSON whose top level is not an object (or whose 'visual' isn't):
      `.get()` raised AttributeError, with the same effect. Since Python 3.12
      even a 3,000-level list parses fine, so the nesting case took this path.
    """
    print("\n[20] Robustness on degenerate input")

    # --- BOM: reading, parsing and preservation on write ---
    bom_path = os.path.join(root, "with_bom.tmdl")
    with open(bom_path, "w", encoding="utf-8-sig") as handle:
        handle.write("table Sales\n\tmeasure X = 1\n")
    lines = tmdl_io.read_tmdl_lines(bom_path)
    blocks = {b.keyword for b in tmdl.find_all_blocks(lines)}
    check("table" in blocks, "the 'table' block stays visible in a file with a BOM", str(blocks))
    check(not lines[0].startswith("\ufeff"), "the BOM doesn't end up in the first line's text")

    tmdl_io.write_tmdl_lines(bom_path, lines)
    with open(bom_path, "rb") as handle:
        check(handle.read(3) == b"\xef\xbb\xbf", "the BOM is preserved on rewrite")

    no_bom_path = os.path.join(root, "without_bom.tmdl")
    with open(no_bom_path, "w", encoding="utf-8") as handle:
        handle.write("table Sales\n")
    tmdl_io.write_tmdl_lines(no_bom_path, tmdl_io.read_tmdl_lines(no_bom_path))
    with open(no_bom_path, "rb") as handle:
        check(handle.read(3) != b"\xef\xbb\xbf", "a file without a BOM doesn't gain one")

    # --- Non-UTF-8 encoding: a meaningful error, not a raw UnicodeDecodeError ---
    cp1252_path = os.path.join(root, "cp1252.tmdl")
    with open(cp1252_path, "wb") as handle:
        handle.write(b"table Caf\xe8\n")
    try:
        tmdl_io.read_tmdl_lines(cp1252_path)
        message = ""
    except tmdl.TmdlError as exc:
        message = str(exc)
    check("UTF-8" in message, "a non-UTF-8 file produces an error that says what to do", message)

    # --- Nested JSON: isolated per file, the scan goes on ---
    ws = os.path.join(root, "ws_degenerate")
    broken = os.path.join(ws, "X.Report", "definition", "pages", "p1", "visuals", "v1")
    good = os.path.join(ws, "X.Report", "definition", "pages", "p2", "visuals", "v2")
    os.makedirs(broken, exist_ok=True)
    os.makedirs(good, exist_ok=True)
    # Deep enough to exceed the parser's limit on every supported Python.
    with open(os.path.join(broken, "visual.json"), "w", encoding="utf-8") as handle:
        handle.write("[" * 200_000 + "]" * 200_000)
    with open(os.path.join(good, "visual.json"), "w", encoding="utf-8") as handle:
        json.dump({"visual": {"visualType": "card"},
                   "query": {"Measure": {"Property": "Total Sales"}}}, handle)
    not_object = os.path.join(ws, "X.Report", "definition", "pages", "p3", "visuals", "v3")
    odd_visual = os.path.join(ws, "X.Report", "definition", "pages", "p4", "visuals", "v4")
    os.makedirs(not_object, exist_ok=True)
    os.makedirs(odd_visual, exist_ok=True)
    # Shallow on purpose: a 3,000-level list parses only from Python 3.12 on,
    # and raises RecursionError on 3.10/3.11, so the check below depended on
    # the interpreter. The deep case is the file above.
    with open(os.path.join(not_object, "visual.json"), "w", encoding="utf-8") as handle:
        handle.write('["not", "an", "object"]')
    with open(os.path.join(odd_visual, "visual.json"), "w", encoding="utf-8") as handle:
        json.dump({"visual": ["not", "an", "object"],
                   "query": {"Measure": {"Property": "Total Sales"}}}, handle)

    from mcp_data_platform_toolkit.reports.report_tools import collect_field_usage
    try:
        outcome = collect_field_usage(ws, [{"kind": "measure", "name": "Total Sales"}])
    except Exception as exc:  # noqa: BLE001
        check(False, "malformed files don't stop the scan", f"{type(exc).__name__}: {exc}")
        return
    labels = sorted(u["label"] for u in outcome["usages"]["Total Sales"])
    check(len(labels) == 2 and any("type: unknown" in label for label in labels),
          "the usages in the readable files are still found, odd 'visual' -> type unknown", str(labels))
    unreadable = outcome["unreadable"]
    check(len(unreadable) == 2 and any("RecursionError" in v for v in unreadable),
          "too deep a file is isolated among the unreadable ones", str(unreadable))
    check(any("ValueError" in v for v in unreadable),
          "a file whose top level is not an object is isolated too", str(unreadable))


def test_regression_long_paths(root: str) -> None:
    """
    The atomic write created a temporary file with a name longer than the
    final one. On a .tmdl path of 249 characters (within Windows' 260 limit)
    the temporary file was 267 characters long and the write failed with a
    FileNotFoundError pointing at a file the user had never seen, while the
    real one was writable.

    Not a lab case: an enterprise PBIP project lives in a synced OneDrive
    folder ('OneDrive - Company Name') and has a deep structure.
    """
    print("\n[21] Long paths (Windows MAX_PATH limit)")

    # A folder nested deep enough to exceed 260 characters overall.
    deep = os.path.join(root, "long")
    while len(deep) < 230:
        deep = os.path.join(deep, "deeply_nested_folder_level")
    path = os.path.join(deep, "TableWithAVeryLongName.tmdl")

    os.makedirs(tmdl_io.extended_path(deep), exist_ok=True)
    content = ["table T\n", "\tmeasure M = 1\n"]
    tmdl_io.write_tmdl_lines(path, content)

    check(len(path) > 260,
          f"the test case really exceeds MAX_PATH ({len(path)} characters)")
    check(tmdl_io.file_fingerprint(path) is not None,
          "the file was created despite the long path")
    check(tmdl_io.read_tmdl_lines(path) == content,
          "the content is read back identical")

    # And the following edit (the path that used to fail) works.
    lines = tmdl_io.read_tmdl_lines(path)
    block = tmdl.find_block(lines, "M", "measure")
    new_lines, _ = tmdl.set_property(lines, block, "displayFolder", "KPI")
    tmdl_io.write_tmdl_lines(path, new_lines, expected_fingerprint=tmdl_io.file_fingerprint(path))
    check("displayFolder: KPI" in "".join(tmdl_io.read_tmdl_lines(path)),
          "an edit on a long path is applied and read back")

    leftovers = [n for n in os.listdir(tmdl_io.extended_path(deep)) if n.startswith(".tmp_")]
    check(not leftovers, "no temporary leftovers on a long path", str(leftovers))


def test_regression_missing_bridge() -> None:
    """
    Without the official server installed NO bridge tool was registered: the
    model didn't know those capabilities existed and, when asked to "create a
    relationship", answered that it wasn't possible without saying why or how
    to fix it. The only warning went to stderr, where MCP clients hide it.
    Now the catalog stays registered and answers with an explanation.
    """
    print("\n[22] Diagnosis when the official server is not installed")
    import asyncio

    from mcp_data_platform_toolkit.powerbi_bridge import bridge

    existing = bridge._discovered_exe_path
    original_which, original_glob = bridge.shutil.which, bridge.glob.glob
    original_path = os.environ.pop("POWERBI_MODELING_MCP_PATH", None)
    try:
        bridge._discovered_exe_path = None
        bridge.shutil.which = lambda *a, **k: None
        bridge.glob.glob = lambda *a, **k: []

        check(not bridge.is_bridge_available(),
              "the missing executable is detected")

        registered: list[str] = []

        class _FakeMcp:
            def add_tool(self, func, name=None, description=None):
                registered.append(name)

        bridge.register_powerbi_modeling_tools(_FakeMcp(), read_only=True, available=False)
        check("list_official_powerbi_tools" in registered,
              "the catalog stays registered even without the executable", str(registered))
        check("call_official_powerbi_tool" not in registered,
              "the write tool is NOT registered", str(registered))

        response = asyncio.run(bridge.list_official_powerbi_tools())
        check("is NOT installed" in response,
              "the tool explains that the official server is missing")
        check(bridge.VSCODE_EXTENSION in response,
              "the response says how to install it")
        check("relationships" in response and "Object-Level Security" in response,
              "the response separates what is missing from what works")
    finally:
        bridge.shutil.which, bridge.glob.glob = original_which, original_glob
        bridge._discovered_exe_path = existing
        if original_path is not None:
            os.environ["POWERBI_MODELING_MCP_PATH"] = original_path


def test_regression_no_silent_cut() -> None:
    """
    `shape_payload` walked down the ladder starting from a rung called "full"
    that nonetheless cut strings at 4,000 characters and returned WITHOUT a
    note: a 9,000-character DAX measure was amputated and the response
    presented itself as complete, without even mentioning the result_id.
    """
    print("\n[23] No undeclared cut in the reduction")
    from mcp_data_platform_toolkit.mcp_common.output_shaping import shape_payload

    long_text = "A" * 9000
    payload = {"name": "Long Measure", "expression": long_text}

    whole = shape_payload(payload, 20000)
    check(long_text in whole, "with a roomy budget the expression arrives intact")

    reduced = shape_payload(payload, 6000)
    check(len(reduced) <= 6000 + 200, "with a tight budget the response respects the budget")
    truncated = long_text not in reduced
    check(not truncated or "response reduced" in reduced,
          "if something was cut the response SAYS so", reduced[-200:])


def test_regression_certain_vs_probable(workspace: str) -> None:
    """
    A textual match (queryRef, visual title) was presented as a usage on a
    par with a real binding. That is how the false positive is born that
    keeps a dead measure from being deleted: a visual titled 'Margin'
    triggers the 'Margin' measure.
    """
    print("\n[24] Impact analysis: certain match distinguished from probable")
    from mcp_data_platform_toolkit.reports.report_tools import collect_field_usage
    from mcp_data_platform_toolkit.reports.pbir_references import MATCH_BINDING, MATCH_TEXT

    outcome = collect_field_usage(workspace, [
        {"kind": "measure", "name": "Total Sales"},
        {"kind": "measure", "name": "Margin %"},
    ])

    total_kinds = {a.get("match") for a in outcome["usages"]["Total Sales"]}
    check(MATCH_BINDING in total_kinds,
          "a real binding is classified as a certain match", str(total_kinds))

    margin = outcome["usages"]["Margin %"]
    check(margin and all(a.get("match") == MATCH_TEXT for a in margin),
          "a queryRef alone is classified as a match to verify",
          str([a.get("match") for a in margin]))

    text = find_measure_usage_in_reports(workspace, measure_names=["Margin %"])
    check("to verify" in text,
          "the output explicitly flags text-only matches", text[:300])


def test_regression_no_reload(project: str) -> None:
    """
    The model used to be re-read and re-parsed on every use: the autodoc
    reloaded it twice FOR EACH documented element, and every lineage call
    read every table file again. Now the model is cached and invalidated by
    the files' fingerprint.
    """
    print("\n[25] No redundant reload of the model")

    loader.clear_model_cache()
    paths.clear_path_cache()

    tables_folder = paths.find_tables_folder(project)
    expected = len([n for n in os.listdir(tables_folder) if n.lower().endswith(".tmdl")])

    reads: list[str] = []
    # Patched where the loader looks it up, the only place that reads table files.
    original = loader.read_tmdl_lines

    def spy(path: str):
        reads.append(path)
        return original(path)

    loader.read_tmdl_lines = spy
    try:
        lineage.build_dax_lineage(project)
        lineage.build_dax_lineage(project, "Margin %")
        autodoc.resolve_autodoc_analysis(project, "Sales", "Margin %", "measure")
    finally:
        loader.read_tmdl_lines = original

    table_reads = [p for p in reads if os.path.dirname(os.path.abspath(p)) ==
                   os.path.abspath(tables_folder)]
    check(len(table_reads) == expected,
          f"each table file is read only once ({len(table_reads)} reads "
          f"for {expected} tables)", str(table_reads))
    check(len(set(table_reads)) == len(table_reads),
          "no table file is re-read across calls")

    # A change on disk invalidates the cache: stale data is never served.
    path = loader.get_table_file_path(project, "Customer")
    time.sleep(0.02)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n\tmeasure 'Cache Probe' = 1\n")
    reloaded = loader.load_semantic_model(project)
    check(any(b.name == "Cache Probe" for b in reloaded["Customer"]["measures"]),
          "a file changed on disk is reloaded")


# ---------------------------------------------------------------------------
# Regressions of the lineage review
# ---------------------------------------------------------------------------
# Same rule as above: every test reproduces a defect observed on the earlier
# code, and says what used to happen.


def _write_files(base: str, files: dict) -> None:
    for relative, content in files.items():
        path = os.path.join(base, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(content, str):
                handle.write(content)
            else:
                json.dump(content, handle)


def test_regression_tmdl_expression_bodies() -> None:
    """
    1. `measure X = ```` (the fenced form Power BI Desktop writes): the
       closing ``` was read as an OPENING fence, and every object after that
       measure disappeared from the file for all the tools.
    2. The body of a multi-line expression was scanned for declarations: an
       M line such as `expression Source = 1` became a phantom object.
    """
    print("\n[26] TMDL: fenced expressions and expression bodies")
    lines = [line + "\n" for line in (
        "table S",
        "\tmeasure 'A' = ```",
        "\t\t\tVAR x = 1",
        "\t\t\tRETURN x",
        "\t\t\t```",
        "\t\tformatString: 0",
        "\tmeasure B = 2",
        "\tcolumn C",
        "\t\tdataType: int64",
        "\tpartition P = m",
        "\t\tsource =",
        "\t\t\t\tlet",
        "\t\t\t\t    expression Source = 1",
        "\t\t\t\tin Source",
    )]
    blocks = {(b.keyword, b.name): b for b in tmdl.find_all_blocks(lines)}
    check(("measure", "B") in blocks and ("column", "C") in blocks,
          "objects after a fenced measure are still found", str(sorted(blocks)))
    fenced = blocks.get(("measure", "A"))
    check(fenced is not None and tmdl.get_expression(lines, fenced) == "VAR x = 1\nRETURN x",
          "the fenced expression is read without the fences",
          repr(tmdl.get_expression(lines, fenced)) if fenced else "")
    check(fenced is not None and tmdl.get_property(lines, fenced, "formatString") is not None,
          "the property after the fence belongs to the measure")
    check(("expression", "Source") not in blocks,
          "an M line inside a partition source is not a declaration", str(sorted(blocks)))

    dax_body = ["table T\n", "\tmeasure X =\n", "\t\t\tRETURN\n", "\t\t\tcolumn\n", "\tcolumn Real\n"]
    names = [(b.keyword, b.name) for b in tmdl.find_all_blocks(dax_body)]
    check(names == [("table", "T"), ("measure", "X"), ("column", "Real")],
          "a DAX line starting with a TMDL keyword is not a declaration", str(names))


def test_regression_dax_references(root: str) -> None:
    """
    The single regular expression used for DAX references produced wrong
    lineage without any warning: `RETURN [X]` became the column RETURN[X],
    `NOT [Flag]` lost the measure, `-- comments` counted as references,
    `Café[Amount]` lost its table, `sales[Amount]` created a table 'sales',
    columns with "blank" in the name were discarded, and `[Qty]` inside
    SUMX(Sales, ...) was attributed to the table hosting the measure.
    """
    print("\n[27] DAX lineage: references resolved against the model")
    project = os.path.join(root, "dax_refs")
    _write_files(project, {
        "M.SemanticModel/definition/tables/Sales.tmdl": (
            "table Sales\n\tmeasure 'Total Sales' = SUM(Sales[Amount])\n\tmeasure 'Is Active' = 1\n"
            "\tcolumn Amount\n\t\tdataType: double\n\tcolumn Qty\n\t\tdataType: int64\n"
            "\tcolumn Price\n\t\tdataType: double\n\tcolumn 'Blank Flag'\n\t\tdataType: string\n"),
        "M.SemanticModel/definition/tables/Café.tmdl": (
            "table Café\n\tcolumn Amount\n\t\tdataType: double\n\tcolumn Qty\n\t\tdataType: int64\n"),
        "M.SemanticModel/definition/tables/Time Intelligence.tmdl": (
            "table 'Time Intelligence'\n\tcalculationGroup\n"
            "\t\tcalculationItem YTD = CALCULATE(SELECTEDMEASURE(), DATESYTD('Calendar'[Date]))\n"
            "\t\tcalculationItem Current = SELECTEDMEASURE()\n"
            "\tcolumn 'Time Calculation'\n\t\tdataType: string\n"),
        "M.SemanticModel/definition/tables/_Measures.tmdl": (
            "table _Measures\n"
            "\tmeasure Ret =\n\t\t\tVAR x = [Total Sales]\n\t\t\tRETURN\n\t\t\t    [Total Sales] * 2\n"
            "\tmeasure Neg = IF(NOT [Is Active], 1)\n"
            "\tmeasure Acc = SUM(Café[Amount])\n"
            "\tmeasure Com = SUM(Sales[Amount]) -- was [Old Measure]\n"
            "\tmeasure Cas = CALCULATE([Total Sales], sales[Amount] > 0)\n"
            "\tmeasure Blk = COUNTROWS(FILTER(Sales, Sales[Blank Flag] = \"x\"))\n"
            "\tmeasure Itr = SUMX(Sales, [Qty] * [Price])\n"
            "\tmeasure Amb = SUM([Qty])\n"
            "\tmeasure Cg = CALCULATE([Total Sales], 'Time Intelligence'[Time Calculation] = \"YTD\")\n"),
    })
    report = lineage.build_dax_lineage(project)
    by_name = {e["name"]: e for e in report["elements"]}

    def deps(name: str) -> dict:
        return by_name.get(name, {})

    check(deps("Ret").get("measures") == ["Sales[Total Sales]"] and not deps("Ret").get("columns"),
          "RETURN [X] is a measure, not a column RETURN[X]", str(deps("Ret")))
    check(deps("Neg").get("measures") == ["Sales[Is Active]"],
          "NOT [X] keeps the dependency on the measure", str(deps("Neg")))
    check(deps("Acc").get("columns") == ["Café[Amount]"],
          "unquoted table name with non-ASCII letters", str(deps("Acc")))
    check(deps("Com").get("columns") == ["Sales[Amount]"],
          "a -- comment is not a reference", str(deps("Com")))
    check(deps("Cas").get("columns") == ["Sales[Amount]"],
          "table names are case-insensitive, as in DAX", str(deps("Cas")))
    check(deps("Blk").get("columns") == ["Sales[Blank Flag]"],
          "a column with 'blank' in its name is not discarded", str(deps("Blk")))
    check(deps("Itr").get("columns") == ["Sales[Price]", "Sales[Qty]"],
          "unqualified columns resolved through the iterated table", str(deps("Itr")))
    check(not deps("Amb").get("columns") and "ambiguous" in " ".join(deps("Amb").get("unresolved", [])),
          "an ambiguous unqualified column is reported, not guessed", str(deps("Amb")))
    check(deps("Cg").get("calc_items") == ["Time Intelligence[YTD]"],
          "calculation item recognized from the value compared with its group column", str(deps("Cg")))
    check(deps("Current").get("selected_measure") is True
          and "Current" not in [e["name"] for e in report["no_dependencies"]],
          "a SELECTEDMEASURE() item is not listed as 'without dependencies'", str(deps("Current")))
    text = lineage.format_dax_lineage(report)
    check("unresolved" in text.lower(), "the output flags unresolved references")


def test_regression_measure_name_unique(root: str) -> None:
    """
    `create_measure` only looked inside the target file: a measure named like
    one of another table was reported as "Created" and the model no longer
    opened in Power BI Desktop.
    """
    print("\n[28] Measure names unique across the model")
    project = os.path.join(root, "unique")
    _write_files(project, {
        "U.SemanticModel/definition/tables/Sales.tmdl": "table Sales\n\tmeasure 'Total Sales' = 1\n",
        "U.SemanticModel/definition/tables/Other.tmdl": "table Other\n\tcolumn Code\n\t\tdataType: string\n",
    })
    other = loader.get_table_file_path(project, "Other")
    before = io.open(other, encoding="utf-8").read()
    for name, expected in (("Total Sales", "already exists in table 'Sales'"),
                           ("code", "column named 'Code'")):
        try:
            writer_tools.create_measure(project, "Other", name, "2")
            message = ""
        except tmdl.TmdlError as exc:
            message = str(exc)
        check(expected in message, f"creation refused: '{name}'", message)
    check(io.open(other, encoding="utf-8").read() == before, "the file was not modified")
    check("Created" in writer_tools.create_measure(project, "Other", "Other Total", "2"),
          "a unique name is still created")


def test_regression_ols_column_permission_forms(root: str) -> None:
    """
    metadataPermission is the DEFAULT property of a columnPermission, so TMDL
    accepts both `columnPermission X = none` and the explicit
    `metadataPermission: none` line. Only the explicit form was read: the
    column was reported as visible, and hiding it again added the explicit
    line under `= none`, declaring the property twice (invalid TMDL).
    """
    print("\n[35] OLS: both TMDL forms of a columnPermission")
    project = os.path.join(root, "ols_forms")
    tables = {
        "O.SemanticModel/definition/tables/Customer.tmdl":
            "table Customer\n\tcolumn Address\n\t\tdataType: string\n"
            "\tcolumn 'Zip Code'\n\t\tdataType: string\n\tcolumn City\n\t\tdataType: string\n",
    }
    roles_path = os.path.join(project, "O.SemanticModel", "definition", "roles.tmdl")

    def setup(roles: str) -> None:
        _write_files(project, {**tables, "O.SemanticModel/definition/roles.tmdl": roles})

    def roles_text() -> str:
        return io.open(roles_path, encoding="utf-8").read()

    def customer() -> dict:
        return ols_roles.get_ols_state(project, "R")["roles"][0]["tables"][0]

    # Inline form, also with an RLS filter on the same table and a quoted name.
    setup("role R\n\tmodelPermission: read\n\n"
          "\ttablePermission Customer = [City] = \"none\"\n\n"
          "\t\tcolumnPermission Address = none\n"
          "\t\tcolumnPermission 'Zip Code' = None\n")
    state = customer()
    check(state["hidden_columns"] == ["Address", "Zip Code"],
          "inline columnPermission read as hidden", str(state))
    check(state["hidden_table"] is False and state["has_row_filter"] is True,
          "the RLS filter of the tablePermission is not read as a permission", str(state))

    before = roles_text()
    result = writer_tools.set_ols_permission(project, "R", "Customer", "Address", hidden=True)
    check("No changes" in result and roles_text() == before,
          "hiding an inline-hidden column changes nothing", result + "\n" + roles_text())

    # A new column follows the file's inline style.
    writer_tools.set_ols_permission(project, "R", "Customer", "City", hidden=True)
    check("columnPermission City = none" in roles_text() and "metadataPermission" not in roles_text(),
          "new columnPermission written in the file's inline style", roles_text())

    # Multi-line and fenced inline values.
    setup("role R\n\ttablePermission Customer\n"
          "\t\tcolumnPermission Address =\n\t\t\t\tnone\n"
          "\t\tcolumnPermission City = ```\n\t\t\t\tread\n\t\t\t\t```\n")
    check(customer()["hidden_columns"] == ["Address"],
          "multi-line inline value read; fenced 'read' stays visible", str(customer()))
    writer_tools.set_ols_permission(project, "R", "Customer", "City", hidden=True)
    text = roles_text()
    check("columnPermission City = none\n" in text and "```" not in text and "read" not in text,
          "a fenced 'read' value is rewritten as '= none'", text)
    check(customer()["hidden_columns"] == ["Address", "City"], "both columns hidden", str(customer()))

    # Duplicate written by earlier versions: reported, then repaired.
    setup("role R\n\ttablePermission Customer\n"
          "\t\tcolumnPermission Address = none\n\t\t\tmetadataPermission: none\n")
    check(customer()["ambiguous_columns"] == ["Address"],
          "property declared twice is flagged", str(customer()))
    check("declared twice" in reader_tools.get_ols_permissions(project, "R"),
          "the text report mentions the duplicate")
    writer_tools.set_ols_permission(project, "R", "Customer", "Address", hidden=True)
    text = roles_text()
    check(text.count("none") == 1 and "metadataPermission" not in text and not customer()["ambiguous_columns"],
          "the duplicate is repaired, a single '= none' remains", text)

    # Visible again: both forms removed, block kept only if something else is in it.
    setup("role R\n\ttablePermission Customer\n"
          "\t\tcolumnPermission Address = none\n"
          "\t\tcolumnPermission City = none\n\t\t\tannotation Owner = Team\n")
    writer_tools.set_ols_permission(project, "R", "Customer", "Address", hidden=False)
    writer_tools.set_ols_permission(project, "R", "Customer", "City", hidden=False)
    text = roles_text()
    check("Address" not in text and "columnPermission City\n" in text and "annotation Owner" in text,
          "inline permission removed; a block with annotations is kept without it", text)
    check(customer()["hidden_columns"] == [], "no column hidden anymore", str(customer()))

    # Explicit-form files keep the explicit style.
    setup("role R\n\ttablePermission Customer\n"
          "\t\tcolumnPermission Address\n\t\t\tmetadataPermission: none\n")
    writer_tools.set_ols_permission(project, "R", "Customer", "City", hidden=True)
    check("columnPermission City\n\t\t\tmetadataPermission: none" in roles_text(),
          "new columnPermission written in the file's explicit style", roles_text())
    check(customer()["hidden_columns"] == ["Address", "City"], "explicit form still read", str(customer()))


def test_regression_autodoc_descriptions(root: str) -> None:
    """
    The technical analysis was written INTO the description
    ('/// Depends on physical columns: Sales[Amount].'), against the
    project's own guidelines, and 'merge' behaved like 'append'.
    """
    print("\n[29] Autodoc: only business text, distinct modes")
    project = os.path.join(root, "autodoc")
    _write_files(project, {
        "A.SemanticModel/definition/tables/Sales.tmdl": (
            "table Sales\n"
            "\t/// Revenue of the period.\n"
            "\t/// Defined in table Sales.\n"
            "\t/// Depends on physical columns: Sales[Amount].\n"
            "\tmeasure Revenue = SUM(Sales[Amount])\n"
            "\tcolumn Amount\n\t\tdataType: double\n"),
    })

    def doc() -> list[str]:
        _, lines = loader.read_table_lines(project, "Sales")
        return [c.strip()[3:].strip() for c in tmdl.get_doc_comments(
            lines, tmdl.find_block(lines, "Revenue", "measure"))]

    preview = reader_tools.get_smart_description_preview(
        project, "Sales", "Revenue", "Total invoiced revenue.", mode="merge")
    check("will be removed" in preview, "the preview announces the cleanup of old lines", preview)

    writer_tools.write_smart_description(project, "Sales", "Revenue", "Total invoiced revenue.", mode="merge")
    check(doc() == ["Revenue of the period.", "Total invoiced revenue."],
          "merge: old technical lines removed, manual text kept, new text added", str(doc()))
    check(not any("[" in line for line in doc()), "no Table[Column] notation in the description")

    writer_tools.write_smart_description(project, "Sales", "Revenue", "Net revenue.", mode="overwrite")
    check(doc() == ["Net revenue."], "overwrite: the description is replaced", str(doc()))

    writer_tools.write_smart_description(project, "Sales", "Revenue", "After returns.", mode="append")
    check(doc() == ["Net revenue.", "After returns."], "append: added after the existing text", str(doc()))


def test_regression_report_coverage(root: str) -> None:
    """
    Usages that the report scan did not see, each reported as "NO usages":
    PBIR-Legacy reports (never read, without saying so), report-level
    measures, bookmarks, drillthrough fields, renamed hierarchy levels,
    auto date/time hierarchies; and namesake measures of ANOTHER model
    counted as usages.
    """
    print("\n[30] Report coverage and model binding")
    from mcp_data_platform_toolkit.reports.report_tools import LEGACY_MESSAGE

    ws = os.path.join(root, "coverage")
    measure = lambda name: {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": name}}
    _write_files(ws, {
        "Model.SemanticModel/definition/tables/Sales.tmdl": (
            "table Sales\n\tmeasure 'Total Sales' = SUM(Sales[Amount])\n\tmeasure 'Margin %' = 1\n"
            "\tcolumn Amount\n\t\tdataType: double\n\tcolumn Region\n\t\tdataType: string\n"
            "\tcolumn OrderDate\n\t\tdataType: dateTime\n"),
        "Model.SemanticModel/definition/tables/Date.tmdl": (
            "table Date\n\tcolumn 'Calendar Year'\n\t\tdataType: int64\n"
            "\thierarchy Calendar\n\t\tlevel Year\n\t\t\tcolumn: 'Calendar Year'\n"),
        "Good.Report/definition.pbir": {"datasetReference": {"byPath": {"path": "../Model.SemanticModel"}}},
        "Good.Report/definition/reportExtensions.json": {"entities": [{"name": "Sales", "measures": [
            {"name": "Margin LY", "expression": "CALCULATE([Margin %], SAMEPERIODLASTYEAR('Date'[Date]))"}]}]},
        "Good.Report/definition/bookmarks/b1.bookmark.json": {
            "displayName": "Focus", "explorationState": {"filters": [{"field": measure("Bookmark Measure")}]}},
        "Good.Report/definition/pages/p1/page.json": {
            "displayName": "Detail", "pageBinding": {"type": "Drillthrough", "parameters": [{"fieldExpr": {
                "Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Region"}}}]}},
        "Good.Report/definition/pages/p1/visuals/v1/visual.json": {"visual": {"visualType": "table", "query": {
            "Select": [{"HierarchyLevel": {"Expression": {"Hierarchy": {
                "Expression": {"SourceRef": {"Entity": "Date"}}, "Hierarchy": "Calendar"}}, "Level": "Year"}}]}}},
        "Good.Report/definition/pages/p1/visuals/v2/visual.json": {"visual": {"visualType": "table", "query": {
            "Select": [{"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"PropertyVariationSource": {
                "Expression": {"SourceRef": {"Entity": "Sales"}}, "Name": "Variation", "Property": "OrderDate"}},
                "Hierarchy": "Date Hierarchy"}}, "Level": "Year"}}]}}},
        "Good.Report/definition/pages/p1/visuals/v3/visual.json": {
            "visual": {"visualType": "card", "query": measure("Total Sales")}},
        "Other.Report/definition.pbir": {"datasetReference": {"byConnection": {
            "connectionString": "Data Source=powerbi://api.powerbi.com/v1.0/myorg/WS;Initial Catalog=OtherModel"}}},
        "Other.Report/definition/pages/p1/visuals/v1/visual.json": {
            "visual": {"visualType": "card", "query": measure("Total Sales")}},
        "Old.Report/report.json": {"sections": [{"visualContainers": [{"config": "{}"}]}]},
    })

    result = find_measure_usage_in_reports(ws, measure_names=["Margin %", "Unused Measure"])
    check("[!] 1 PBIR-Legacy report NOT analyzed, upgrade to PBIR: Old" in result,
          "a PBIR-Legacy report is declared NOT analyzed", result)
    check("NO usages found in the analyzed reports" in result,
          "'NO usages' is qualified when a report was not analyzed", result)
    check("'Margin %': 1 usage in the analyzed reports" in result,
          "a positive count is qualified too: the legacy report may hold more", result)
    check("2 fields searched, 2 reports analyzed" in result,
          "the header counts fields and reports with the right plural", result.splitlines()[0])
    check("report-level measure 'Margin LY'" in result,
          "a model measure used only by a report-level measure is found", result)

    bookmark = find_measure_usage_in_reports(ws, "Bookmark Measure")
    check("bookmark 'b1'" in bookmark, "a measure used only in a bookmark is found", bookmark)

    drill = find_measure_usage_in_reports(ws, column_names=["Sales[Region]"])
    check("drillthrough" in drill, "a drillthrough field is found", drill)

    level = find_measure_usage_in_reports(ws, column_names=["Date[Calendar Year]", "Date[Year]"])
    check("'Date[Calendar Year]': 1 usage" in level,
          "a renamed hierarchy level is traced to its column through the model", level)
    check("'Date[Year]': NO usages" in level,
          "the level name alone is not a column", level)

    variation = find_measure_usage_in_reports(ws, column_names=["Sales[OrderDate]"])
    check("'Sales[OrderDate]': 1 usage" in variation,
          "an auto date/time hierarchy counts as a usage of its date column", variation)

    unfiltered = find_measure_usage_in_reports(ws, "Total Sales")
    filtered = find_measure_usage_in_reports(
        ws, "Total Sales", semantic_model=os.path.join(ws, "Model.SemanticModel"))
    check("'Total Sales': 2 usages" in unfiltered, "without a model filter both reports count", unfiltered)
    check("'Total Sales': 1 usage" in filtered and "Other -> OtherModel" in filtered,
          "with a model filter the report of another model is skipped and named", filtered)

    only_legacy = os.path.join(root, "only_legacy")
    _write_files(only_legacy, {"Old.Report/report.json": {"sections": []}})
    try:
        find_measure_usage_in_reports(only_legacy, "Total Sales")
        message = ""
    except ValueError as exc:
        message = str(exc)
    check(LEGACY_MESSAGE in message,
          "with only PBIR-Legacy reports the tool fails instead of answering 'NO usages'", message)

    # A 'definition' folder with nothing readable in it (half-converted,
    # emptied) was counted as analyzed: zero files read, silent "NO usages".
    empty = os.path.join(root, "empty_definition")
    os.makedirs(os.path.join(empty, "Empty.Report", "definition"))
    _write_files(empty, {"Full.Report/definition/pages/p1/visuals/v1/visual.json": {
        "visual": {"visualType": "card", "query": measure("Total Sales")}}})
    partial = find_measure_usage_in_reports(empty, "Total Sales")
    check("[!] 1 report folder NOT analyzed, no report definition found: Empty" in partial,
          "a PBIR folder with no readable file is declared NOT analyzed", partial)
    check("1 report analyzed" in partial and "1 usage in the analyzed reports" in partial,
          "only the report actually read counts as analyzed", partial)

    shutil.rmtree(os.path.join(empty, "Full.Report"))
    try:
        find_measure_usage_in_reports(empty, "Total Sales")
        message = ""
    except ValueError as exc:
        message = str(exc)
    check("no report definition found" in message,
          "with only unreadable report folders the tool fails", message)


def test_regression_coverage_survives_reduction() -> None:
    """
    The "NOT analyzed" warning is what makes every count honest: if the
    output reduction dropped it, the counts would look complete again.
    """
    print("\n[34] The coverage warning survives output reduction")
    from mcp_data_platform_toolkit.reports.report_tools import LEGACY_MESSAGE, _format_usages

    report = {
        "fields": [f"Measure {i:03d}" for i in range(30)],
        "usages": {
            f"Measure {i:03d}": [
                {"report": "R", "page": f"Page {p}", "kind": "visual",
                 "label": f"R | Page {p} | visual 'v{p}' (type: table)"}
                for p in range(i % 5)
            ]
            for i in range(30)
        },
        "unreadable": [],
        "reports": {"analyzed": [("R", {})], "legacy": ["Old Sales"], "unknown_format": [],
                    "other_model": [], "unverified": []},
    }
    full = _format_usages(report, max_chars=None)
    for budget in (2000, 500):
        reduced = _format_usages(report, max_chars=budget)
        check(len(reduced) < len(full), f"budget {budget}: the output really was reduced")
        check(LEGACY_MESSAGE in reduced and "Old Sales" in reduced,
              f"budget {budget}: the NOT analyzed warning is still there", reduced[:300])
        check("in the analyzed reports" in reduced,
              f"budget {budget}: the counts that are left are still qualified", reduced[:300])


def test_regression_relationship_cardinality(root: str) -> None:
    """
    Every relationship was printed as '*--1' and its from side taken as a
    fact table, one-to-one and many-to-many included.
    """
    print("\n[31] Relationship cardinality")
    project = os.path.join(root, "cardinality")
    _write_files(project, {
        "C.SemanticModel/definition/tables/A.tmdl": "table A\n\tcolumn K\n\t\tdataType: int64\n",
        "C.SemanticModel/definition/tables/B.tmdl": "table B\n\tcolumn K\n\t\tdataType: int64\n",
        "C.SemanticModel/definition/relationships.tmdl": (
            "relationship r1\n\ttoCardinality: one\n\tfromCardinality: one\n"
            "\tfromColumn: A.K\n\ttoColumn: B.K\n\n"
            "relationship r2\n\tfromCardinality: many\n\ttoCardinality: many\n"
            "\tfromColumn: B.K\n\ttoColumn: A.K\n"),
    })
    text = reader_tools.get_relationships(project)
    check("A[K] 1--1 B[K]" in text and "B[K] *--* A[K]" in text,
          "the real cardinality is shown", text)
    check(not rels.is_fact_table(project, "A") and not rels.is_fact_table(project, "B"),
          "1:1 and many-to-many don't make a fact table")


def test_regression_table_name_not_a_file_name(root: str) -> None:
    """
    A table whose name isn't a valid file name ('Ratio: A/B') was
    unreachable: the name was rejected as a path before being looked up.
    """
    print("\n[32] Tables whose name is not a valid file name")
    project = os.path.join(root, "odd_names")
    _write_files(project, {
        "O.SemanticModel/definition/tables/Ratio A B.tmdl": (
            "table 'Ratio: A/B'\n\tmeasure R = 1\n"),
    })
    path = loader.get_table_file_path(project, "Ratio: A/B")
    check(path.endswith("Ratio A B.tmdl"), "the table is found by its declared name", path)
    check("Ratio: A/B" in reader_tools.list_tables(project) and "Ratio A B.tmdl" in reader_tools.list_tables(project),
          "list_tables shows the declared name and the file name")
    try:
        loader.get_table_file_path(project, "../Ratio A B")
        rejected = False
    except (paths.PathNotAllowedError, FileNotFoundError):
        rejected = True
    check(rejected, "a path-like name that is not a declared table is still rejected")


def test_regression_bridge_paths(project: str) -> None:
    """
    The bridge validated operation names only: an allowed operation with an
    explicit folder argument could export the model outside the project.
    """
    print("\n[33] Bridge: path arguments confined to the model")
    from mcp_data_platform_toolkit.powerbi_bridge.bridge import paths_outside_model

    model = paths.find_semantic_model_folder(project)
    inside = os.path.join(model, "definition")
    check(not paths_outside_model({"request": {"operation": "ExportToTmdlFolder"}}, model),
          "no path argument: allowed")
    check(not paths_outside_model({"request": {"tmdlFolderPath": inside}}, model),
          "a path inside the model: allowed")
    for arguments in (
        {"request": {"operation": "ExportToTmdlFolder", "tmdlFolderPath": os.path.dirname(model)}},
        {"request": {"folderPath": "relative\\folder"}},
        {"request": json.dumps({"operation": "ImportFromTmdlFolder", "folderPath": "C:\\elsewhere"})},
    ):
        check(bool(paths_outside_model(arguments, model)), f"refused: {str(arguments)[:70]}")


# ---------------------------------------------------------------------------
# Regressions of the code review that preceded the end-to-end lineage
# ---------------------------------------------------------------------------
# Same rule as above: every test reproduces a defect observed on the earlier
# code, and says what used to happen.


def _expect_error(action, error_types=(Exception,)) -> str:
    """Runs `action` and returns the message of the expected error ('' if none was raised)."""
    try:
        action()
    except error_types as exc:  # noqa: BLE001 - it is what is being checked
        return str(exc) or type(exc).__name__
    return ""


def test_regression_fence_and_control_characters(root: str) -> None:
    """
    1. A DAX expression with a ``` line was written as is: from that line on
       the parser read the rest of the file as a fenced expression, and every
       column, partition and annotation after the new measure disappeared,
       while `create_measure` answered "Created".
    2. Control characters in an expression or a description (NUL, BEL) were
       written into the file: only names and property values were checked.
    """
    print("\n[36] Expressions and descriptions that would corrupt the file")
    project = os.path.join(root, "fence")
    _write_files(project, {
        "F.SemanticModel/definition/tables/Sales.tmdl": (
            "table Sales\n\tmeasure Base = 1\n\n\tcolumn Amount\n\t\tdataType: double\n\n"
            "\tpartition P = m\n\t\tmode: import\n\t\tsource = let x = 1 in x\n"),
    })
    path = loader.get_table_file_path(project, "Sales")
    before = io.open(path, encoding="utf-8").read()
    blocks_before = {(b.keyword, b.name) for b in tmdl.find_all_blocks(tmdl_io.read_tmdl_lines(path))}

    attempts = (
        ("a ``` line in the expression", lambda: writer_tools.create_measure(
            project, "Sales", "Fenced", "VAR x = 1\n```\nRETURN x")),
        ("``` at the end of a single-line expression", lambda: writer_tools.create_measure(
            project, "Sales", "Fenced", "1 // ```")),
        ("BEL in the expression", lambda: writer_tools.create_measure(project, "Sales", "Bell", "1\x07")),
        ("NUL in the description", lambda: writer_tools.create_measure(
            project, "Sales", "Nul", "1", description="desc\x00ok")),
        ("NUL in a smart description", lambda: writer_tools.write_smart_description(
            project, "Sales", "Base", "Business text\x00")),
        ("``` in a property value", lambda: writer_tools.set_display_folder(
            project, "Sales", "Base", display_folder="x = ```")),
    )
    for label, action in attempts:
        message = _expect_error(action, (tmdl.TmdlError,))
        check(bool(message), f"refused: {label}", message)
    check(io.open(path, encoding="utf-8").read() == before, "the file was not modified by any attempt")
    blocks_after = {(b.keyword, b.name) for b in tmdl.find_all_blocks(tmdl_io.read_tmdl_lines(path))}
    check(blocks_after == blocks_before, "every object is still visible", str(blocks_after ^ blocks_before))

    # What stays legitimate: tabs, CRLF line breaks, a U+2028 inside a DAX string.
    writer_tools.create_measure(project, "Sales", "Tabbed", "VAR x = 1\r\nRETURN\tx",
                                description="First line\r\nSecond line")
    writer_tools.create_measure(project, "Sales", "Separator", 'IF(1, "a\u2028b")')
    _, lines = loader.read_table_lines(project, "Sales")
    tabbed = tmdl.find_block(lines, "Tabbed", "measure")
    check(tmdl.get_expression(lines, tabbed) == "VAR x = 1\nRETURN\tx",
          "a tab and a CRLF line break are kept", repr(tmdl.get_expression(lines, tabbed)))
    check([c.strip() for c in tmdl.get_doc_comments(lines, tabbed)] == ["/// First line", "/// Second line"],
          "a description over two lines becomes two '///' lines", str(tmdl.get_doc_comments(lines, tabbed)))
    separator = tmdl.find_block(lines, "Separator", "measure")
    check(tmdl.get_expression(lines, separator) == 'IF(1, "a\u2028b")',
          "U+2028 inside a DAX string stays on one line", repr(tmdl.get_expression(lines, separator)))
    check(("column", "Amount") in {(b.keyword, b.name) for b in tmdl.find_all_blocks(lines)},
          "the objects after the new measures are still visible")


def test_regression_pbip_identifies_its_model(root: str) -> None:
    """
    A .pbip file stood only for the folder containing it: with two projects
    side by side the tool answered "which model?", and the question advised
    passing the .pbip file, i.e. the very call that had just been made.
    """
    print("\n[37] A .pbip file identifies its own semantic model")
    from mcp_data_platform_toolkit.mcp_common.errors import tool_error_handler

    workspace = os.path.join(root, "pbip_side_by_side")
    _write_files(workspace, {
        # Named like Power BI Desktop names it: '<stem>.SemanticModel'.
        "Alpha.pbip": {"version": "1.0", "artifacts": []},
        "Alpha.SemanticModel/definition/tables/A.tmdl": "table A\n\tcolumn C\n\t\tdataType: string\n",
        # Found through the report: .pbip -> report -> definition.pbir byPath.
        "Beta.pbip": {"version": "1.0", "artifacts": [{"report": {"path": "Beta Report.Report"}}]},
        "Beta Report.Report/definition.pbir": {"datasetReference": {"byPath": {"path": "../Beta Data.SemanticModel"}}},
        "Beta Data.SemanticModel/definition/tables/B.tmdl": "table B\n\tcolumn C\n\t\tdataType: string\n",
        # A report connected to a published model: no local model at all.
        "Thin.pbip": {"version": "1.0", "artifacts": [{"report": {"path": "Thin.Report"}}]},
        "Thin.Report/definition.pbir": {"datasetReference": {"byConnection": {
            "connectionString": "Data Source=powerbi://api.powerbi.com/v1.0/myorg/WS;Initial Catalog=Remote"}}},
    })
    paths.clear_path_cache()
    tool = tool_error_handler(reader_tools.list_tables)

    alpha = tool(os.path.join(workspace, "Alpha.pbip"))
    check("A" in alpha.splitlines() and "Which one" not in alpha,
          "the .pbip finds the model named after it", alpha[:160])
    beta = tool(os.path.join(workspace, "Beta.pbip"))
    check("B" in beta.splitlines() and "Which one" not in beta,
          "the .pbip finds the model its report is bound to", beta[:160])

    message = _expect_error(lambda: paths.find_semantic_model_folder(os.path.join(workspace, "Thin.pbip")),
                            (FileNotFoundError,))
    check("no local semantic model" in message,
          "a report connected to a published model doesn't borrow a neighbour's model", message)

    question = tool(workspace)
    check("Which one" in question and ".SemanticModel' folder" in question,
          "the folder is still ambiguous, and the advice names the .SemanticModel folder", question[-200:])


def test_regression_qualified_references_exist(root: str) -> None:
    """
    A qualified reference was taken as written: `Nope[Amount]`,
    `Sales[NoSuchColumn]` and `t[x]` on a table variable all became physical
    columns, i.e. phantom nodes of the lineage.
    """
    print("\n[38] DAX lineage: qualified references must exist in the model")
    from mcp_data_platform_toolkit.semantic_model import dax_lexer

    project = os.path.join(root, "qualified")
    _write_files(project, {
        "Q.SemanticModel/definition/tables/Sales.tmdl": (
            "table Sales\n\tmeasure 'Total Sales' = SUM(Sales[Amount])\n"
            "\tcolumn Amount\n\t\tdataType: double\n\tcolumn OrderDate\n\t\tdataType: dateTime\n"),
        "Q.SemanticModel/definition/tables/Month.tmdl": "table Month\n\tcolumn Amount\n\t\tdataType: double\n",
        "Q.SemanticModel/definition/tables/_Measures.tmdl": (
            "table _Measures\n"
            "\tmeasure Ghost = SUM(Nope[Amount])\n"
            "\tmeasure Missing = SUM(Sales[NoSuchColumn])\n"
            "\tmeasure Var = VAR t = ADDCOLUMNS(Sales, \"x\", 1) RETURN SUMX(t, t[x])\n"
            "\tmeasure Wrong = _Measures[Total Sales]\n"
            "\tmeasure Right = 'Sales'[Total Sales] + SUM(sales[amount])\n"
            "\tmeasure Variation = MAX(Sales[OrderDate].[Year])\n"
            "\tmeasure Shift = CALCULATE(SUMX(Sales, [Amount]), DATEADD(Sales[OrderDate], -1, MONTH))\n"),
    })
    by_name = {e["name"]: e for e in lineage.build_dax_lineage(project)["elements"]}

    def deps(name: str) -> dict:
        return by_name.get(name, {})

    check(not deps("Ghost").get("columns")
          and any("Nope[Amount]" in u and "table not found" in u for u in deps("Ghost").get("unresolved", [])),
          "a table missing from the model is unresolved, not a column", str(deps("Ghost")))
    check(not deps("Missing").get("columns")
          and any("NoSuchColumn" in u for u in deps("Missing").get("unresolved", [])),
          "a column missing from its table is unresolved", str(deps("Missing")))
    check("Var" not in by_name or not (deps("Var").get("columns") or deps("Var").get("unresolved")),
          "the column of a table variable is local to the expression", str(deps("Var")))
    check(any("not of '_Measures'" in u for u in deps("Wrong").get("unresolved", [])),
          "a measure qualified with the wrong table is unresolved", str(deps("Wrong")))
    check(deps("Right").get("measures") == ["Sales[Total Sales]"]
          and deps("Right").get("columns") == ["Sales[Amount]"],
          "existing references resolved, with the names as declared", str(deps("Right")))
    check(deps("Variation").get("columns") == ["Sales[OrderDate]"] and not deps("Variation").get("unresolved"),
          "an auto date/time level (.[Year]) is not an unresolved reference", str(deps("Variation")))
    check(deps("Shift").get("columns") == ["Sales[Amount]", "Sales[OrderDate]"],
          "MONTH is a DATEADD interval, not the table 'Month'", str(deps("Shift")))
    check("MONTH" not in dax_lexer.analyze("DATEADD('Date'[Date], -1, MONTH)").tables,
          "bare DAX constants are not table names")


def test_regression_inline_expression_continued() -> None:
    """
    An expression starting on the declaration line and continuing on deeper
    lines (`measure A = CALCULATE(`): only 'CALCULATE(' was read, every
    reference after it was lost, and the continuation lines were scanned for
    declarations.
    """
    print("\n[39] TMDL: inline expression continued on the following lines")
    lines = [line + "\n" for line in (
        "table T",
        "\tmeasure 'A' = CALCULATE(",
        "\t\t\t\t[B],",
        "\t\t\t\tcolumn",
        "\t\t\t)",
        "\t\tformatString: 0",
        "\tmeasure B = 1",
        "\tcolumn Col",
        "\t\tdataType: int64",
    )]
    blocks = [(b.keyword, b.name) for b in tmdl.find_all_blocks(lines)]
    check(blocks == [("table", "T"), ("measure", "A"), ("measure", "B"), ("column", "Col")],
          "a continuation line starting with a keyword is not a declaration", str(blocks))
    measure = tmdl.find_block(lines, "A", "measure")
    expression = tmdl.get_expression(lines, measure)
    check(expression == "CALCULATE(\n\t[B],\n\tcolumn\n)",
          "the whole expression is read", repr(expression))
    check(tmdl.get_property(lines, measure, "formatString") is not None,
          "the property after the continuation still belongs to the measure")

    roles = ["role R\n", "\ttablePermission T\n", "\t\tcolumnPermission Col = no\n", "\t\t\t\tne\n"]
    permission = tmdl.find_block(roles, "Col", "columnPermission")
    rewritten = tmdl.set_default_value(roles, permission, "none")
    check(rewritten[2:] == ["\t\tcolumnPermission Col = none\n"],
          "rewriting the default value replaces the continuation lines too", str(rewritten))


def test_regression_property_targets(root: str) -> None:
    """
    Properties written where TOM has none produced files Power BI Desktop
    refuses, while the tools answered "Updated": a calculation item has no
    isHidden, displayFolder or formatString (its format is the DAX
    'formatStringDefinition'), a table has no displayFolder. Making an
    element visible wrote 'isHidden: false' instead of removing the line.
    """
    print("\n[40] Properties written only where they exist")
    project = os.path.join(root, "targets")
    _write_files(project, {
        "P.SemanticModel/definition/tables/Sales.tmdl": (
            "table Sales\n\tmeasure M = 1\n\t\tisHidden\n\n\tcolumn Amount\n\t\tdataType: double\n"),
        "P.SemanticModel/definition/tables/TI.tmdl": (
            "table TI\n\tcalculationGroup\n"
            "\t\tcalculationItem Old = SELECTEDMEASURE()\n\t\t\tformatString: 0.0\n"
            "\tcolumn Name\n\t\tdataType: string\n"),
    })
    ti_path = loader.get_table_file_path(project, "TI")
    ti_before = io.open(ti_path, encoding="utf-8").read()
    for label, action in (
        ("displayFolder on a calculation item", lambda: writer_tools.set_display_folder(
            project, "TI", "Old", "measure", "Folder")),
        ("isHidden on a calculation item", lambda: writer_tools.set_item_visibility(
            project, "TI", "Old", "calculationItem", True)),
        ("a new calculation item with a display folder", lambda: writer_tools.create_calculation_item(
            project, "TI", "New", "SELECTEDMEASURE()", display_folder="Folder")),
        ("a new hidden calculation item", lambda: writer_tools.create_calculation_item(
            project, "TI", "New", "SELECTEDMEASURE()", is_hidden=True)),
    ):
        check(bool(_expect_error(action, (tmdl.TmdlError,))), f"refused: {label}")
    check(io.open(ti_path, encoding="utf-8").read() == ti_before, "the calculation group file was not modified")
    check(bool(_expect_error(lambda: writer_tools.set_display_folder(
        project, "Sales", "Sales", display_folder="Folder", is_table=True), (tmdl.TmdlError,))),
        "refused: displayFolder on a table")

    writer_tools.format_measure(project, "TI", "Old", "0.00%")
    writer_tools.create_calculation_item(project, "TI", "Pct", "SELECTEDMEASURE()", format_string='0.0 "pts"')
    _, lines = loader.read_table_lines(project, "TI")
    old = tmdl.find_block(lines, "Old", "calculationItem")
    old_text = "".join(lines[old.start:old.end])
    check('formatStringDefinition = "0.00%"' in old_text and "formatString:" not in old_text,
          "format of a calculation item written as formatStringDefinition, the invalid line removed", old_text)
    pct = tmdl.find_block(lines, "Pct", "calculationItem")
    check('formatStringDefinition = "0.0 ""pts"""' in "".join(lines[pct.start:pct.end]),
          "a new calculation item gets a formatStringDefinition, quotes doubled",
          "".join(lines[pct.start:pct.end]))
    again = writer_tools.format_measure(project, "TI", "Old", "0.00%")
    check("No changes" in again, "setting the same format again changes nothing", again)

    writer_tools.set_item_visibility(project, "Sales", "M", "measure", False)
    writer_tools.create_measure(project, "Sales", "Visible", "2", is_hidden=False)
    text = io.open(loader.get_table_file_path(project, "Sales"), encoding="utf-8").read()
    check("isHidden" not in text, "visible means no isHidden line, not 'isHidden: false'", text)
    writer_tools.set_item_visibility(project, "Sales", "Amount", "column", True)
    _, lines = loader.read_table_lines(project, "Sales")
    check(tmdl.is_hidden(lines, tmdl.find_block(lines, "Amount", "column")),
          "hiding a column still works")


def test_regression_fact_table_override(project: str) -> None:
    """The fact-table protection had no override, unlike the one on hidden elements."""
    print("\n[41] Fact-table columns: protected by default, not forbidden")
    preview = reader_tools.get_smart_description_preview(
        project, "Sales", "CustomerKey", "Customer of the sale.", element_type="column")
    check("allow_fact_table_columns" in preview, "the preview announces the protection", preview[-200:])
    result = writer_tools.write_smart_description(
        project, "Sales", "CustomerKey", "Customer of the sale.", element_type="column",
        allow_fact_table_columns=True)
    check("Smart-Doc" in result, "with the override the column is documented", result)


def test_regression_ols_names_and_race(root: str) -> None:
    """
    1. The roles file's fingerprint was taken AFTER reading it: a change
       landing in between was overwritten without the "changed after it was
       read" refusal.
    2. Permissions were written with the names as typed ('sales', 'cost').
    3. A role name with ':' was refused even where no file name is built.
    """
    print("\n[42] OLS: concurrent change, declared names, role names")
    project = os.path.join(root, "ols_names")
    _write_files(project, {
        "O.SemanticModel/definition/tables/Sales.tmdl": "table Sales\n\tcolumn Cost\n\t\tdataType: double\n",
        "O.SemanticModel/definition/roles.tmdl": "role R\n\tmodelPermission: read\n",
    })
    roles_path = os.path.join(project, "O.SemanticModel", "definition", "roles.tmdl")

    original = ols_roles.read_tmdl_lines

    def read_then_someone_writes(path: str):
        lines = original(path)
        if os.path.abspath(path) == os.path.abspath(roles_path):
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\nrole Concurrent\n\tmodelPermission: read\n")
        return lines

    ols_roles.read_tmdl_lines = read_then_someone_writes
    try:
        message = _expect_error(lambda: ols_roles.set_ols_permission(project, "R", "Sales"), (tmdl.TmdlError,))
    finally:
        ols_roles.read_tmdl_lines = original
    check("changed after it was read" in message, "a change right after the read is detected", message)
    check("role Concurrent" in io.open(roles_path, encoding="utf-8").read(),
          "the concurrent change was not overwritten")

    ols_roles.set_ols_permission(project, "R", "sales", "cost")
    text = io.open(roles_path, encoding="utf-8").read()
    check("tablePermission Sales" in text and "columnPermission Cost" in text,
          "table and column written as declared", text)

    ols_roles.set_ols_permission(project, "Sales: EMEA", model_permission="read")
    check("role 'Sales: EMEA'" in io.open(roles_path, encoding="utf-8").read(),
          "a role name with ':' is accepted and quoted")

    os.remove(roles_path)
    roles_folder = os.path.join(project, "O.SemanticModel", "definition", "roles")
    os.makedirs(roles_folder)
    for name in ("Sales: EMEA", "Sales_ EMEA", "CON"):
        ols_roles.set_ols_permission(project, name, model_permission="read")
    files = sorted(os.listdir(roles_folder))
    check(files == ["Sales_ EMEA (2).tmdl", "Sales_ EMEA.tmdl", "_CON.tmdl"],
          "folder layout: safe file names, an existing file is never reused", str(files))
    check(sorted(ols_roles.list_role_names(project)) == ["CON", "Sales: EMEA", "Sales_ EMEA"],
          "each role keeps its own name", str(ols_roles.list_role_names(project)))


def test_regression_bridge_robustness(project: str) -> None:
    """
    1. Every key ending in 'folder' was a filesystem path: a measure with a
       'displayFolder' could not be created or updated through the bridge.
    2. A nested '"success": false' failed the whole call, and the write that
       did succeed was not saved.
    3. Read-only operations listed although always blocked by a fragment.
    4. Timeouts and transport errors escaped from inside the session: the
       cause was lost in "unhandled errors in a TaskGroup".
    """
    print("\n[43] Bridge: display folders, outcomes, timeouts")
    import asyncio
    import contextlib
    import types

    from mcp_data_platform_toolkit.powerbi_bridge import bridge

    model = paths.find_semantic_model_folder(project)
    allowed = (
        {"request": {"operation": "Update", "definitions": [{"name": "M", "displayFolder": "KPI\\Sales"}]}},
        {"request": {"operation": "Create", "definitions": [{"name": "Q", "folder": "Staging\\Raw"}]}},
    )
    for arguments in allowed:
        check(not bridge.paths_outside_model(arguments, model), f"allowed: {str(arguments)[:70]}")
    for value in ("C:\\elsewhere", "..\\..\\out"):
        check(bool(bridge.paths_outside_model({"request": {"folder": value}}, model)),
              f"a filesystem-looking value under a display-folder key is still checked ({value!r})")
    check(not [op for op in bridge.READ_ONLY_OPERATIONS if bridge.forbidden_operations({"operation": op})],
          "no read-only operation is blocked by the forbidden fragments")

    def result(text: str, is_error: bool = False):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)], is_error=is_error)

    check(not bridge._failed(result('{"success": true, "results": [{"success": false}]}')),
          "a nested failure doesn't fail the whole call")
    check(not bridge._failed(result('{"results": [{"success": true}, {"success": false}]}')),
          "a partly failed batch is not a failed call: its successes must be saved")
    check(bridge._failed(result('{"results": [{"success": false}, {"success": false}]}')),
          "a batch whose items all failed is a failed call")
    check(bridge._failed(result('{"success": false, "message": "x"}')), "a top-level failure is a failure")
    check(bridge._failed(result("ok", is_error=True)), "isError is a failure")
    check(bridge._failed(result('Operation failed: "success": false')), "non-JSON text with the marker")

    class FakeClient:
        def __init__(self, slow: str):
            self.slow, self.calls = slow, []

        async def list_tools(self):
            tool = types.SimpleNamespace(name="measure_operations", description="", input_schema={})
            return types.SimpleNamespace(tools=[tool])

        async def call_tool(self, name, arguments):
            operation = arguments["request"]["operation"]
            self.calls.append(operation)
            if operation == self.slow:
                await asyncio.sleep(5)
            return result('{"success": true}')

    class Group(Exception):
        def __init__(self, inner: Exception):
            super().__init__("unhandled errors in a TaskGroup (1 sub-exception)")
            self.exceptions = [inner]

    saved = (bridge._open_session, bridge.CALL_TIMEOUT_SECONDS, bridge._tool_catalog_cache,
             bridge._missing_expected_tools)
    arguments = {"request": {"operation": "Create", "definitions": [{"name": "X"}]}}
    try:
        bridge.CALL_TIMEOUT_SECONDS = 0.05
        for slow, expected, label in (
            ("Create", "did not complete", "the operation times out"),
            (bridge.SAVE_OPERATION, "may NOT have been written", "saving to disk times out"),
        ):
            client = FakeClient(slow)
            bridge._tool_catalog_cache = None

            @contextlib.asynccontextmanager
            async def fake_session(client=client):
                yield client

            bridge._open_session = fake_session
            message = _expect_error(lambda: asyncio.run(
                bridge.call_official_powerbi_tool(project, "measure_operations", arguments)), (RuntimeError,))
            check(expected in message and "TimeoutError" in message, f"{label}: the cause is reported", message)
            if slow == "Create":
                check(bridge.SAVE_OPERATION not in client.calls, "nothing is saved after a failed operation",
                      str(client.calls))

        @contextlib.asynccontextmanager
        async def broken_session():
            raise Group(ConnectionResetError("pipe closed"))
            yield  # pragma: no cover

        bridge._open_session = broken_session
        message = _expect_error(lambda: asyncio.run(
            bridge.call_official_powerbi_tool(project, "measure_operations", arguments)), (RuntimeError,))
        check("ConnectionResetError: pipe closed" in message,
              "an exception group is unwrapped down to its cause", message)
    finally:
        (bridge._open_session, bridge.CALL_TIMEOUT_SECONDS, bridge._tool_catalog_cache,
         bridge._missing_expected_tools) = saved


def test_regression_output_shaping_details() -> None:
    """
    1. Every response was cached, small ones too, evicting the entries that
       reduced responses point to.
    2. Non-JSON text was flattened into a single line.
    3. Two errors under the same key: only the last one survived the stub.
    """
    print("\n[44] Output shaping: cache, text, diagnostics")
    from mcp_data_platform_toolkit.mcp_common import output_shaping as shaping

    before = len(shaping._RESULT_CACHE)
    small = shaping.shape_output('{"name": "M", "expression": "1"}', max_chars=1000, tool_name="t")
    check(len(shaping._RESULT_CACHE) == before and '"name":"M"' in small,
          "a response that fits is not cached", str(len(shaping._RESULT_CACHE)))

    table = "Name | Type\nSales | table\nCustomer | table"
    check(shaping.shape_output(table, max_chars=1000) == table, "multi-line text keeps its lines")
    long_text = "\n".join(f"line {i:04d} of the log" for i in range(500))
    cut = shaping.shape_output(long_text, max_chars=600)
    check(cut.startswith("[!] INCOMPLETE RESPONSE") and "\nline 0000 of the log\n" in cut and len(cut) <= 600,
          "long multi-line text is cut at a line boundary, with the warning on top", cut[:120])

    diagnostics = shaping.extract_diagnostics({"items": [{"error": "first failure"}, {"error": "second failure"}]})
    check(diagnostics.get("error") == ["first failure", "second failure"],
          "two errors under the same key are both kept", str(diagnostics))


def test_regression_report_binding_and_measures(root: str) -> None:
    """
    1. A model with the same NAME in another folder counted as the model the
       search was restricted to.
    2. Every bracketed name of a report-level measure counted as a CERTAIN
       measure: `SUM(Sales[Amount])` made a measure 'Amount' look used.
    """
    print("\n[45] Reports: model binding and report-level measures")
    from mcp_data_platform_toolkit.reports.report_tools import _binding_matches, collect_field_usage
    from mcp_data_platform_toolkit.reports.pbir_references import MATCH_BINDING, MATCH_TEXT

    elsewhere = os.path.join(root, "binding", "other", "Model.SemanticModel")
    mine = os.path.join(root, "binding", "mine", "Model.SemanticModel")
    os.makedirs(elsewhere)
    os.makedirs(mine)
    flt = {"folder": mine, "name": "Model"}
    check(_binding_matches({"mode": "byPath", "model_folder": elsewhere, "model_name": "Model"}, flt) is False,
          "a namesake model in another folder is another model")
    check(_binding_matches({"mode": "byPath", "model_folder": mine, "model_name": "Model"}, flt) is True,
          "the same folder is the same model")
    missing = os.path.join(root, "binding", "gone", "Model.SemanticModel")
    check(_binding_matches({"mode": "byPath", "model_folder": missing, "model_name": "Model"}, flt) is None,
          "a bound folder that isn't on disk can't be verified")
    check(_binding_matches({"mode": "byPath", "model_folder": elsewhere, "model_name": "Model"},
                           {"folder": "", "name": "model"}) is True,
          "a filter given by name still matches by name")

    ws = os.path.join(root, "report_measures")
    _write_files(ws, {"R.Report/definition/reportExtensions.json": {"entities": [{"name": "Sales", "measures": [
        {"name": "Twice", "expression": "SUM(Sales[Amount]) * [Factor]"}]}]}})
    usage = collect_field_usage(ws, [
        {"kind": "measure", "name": "Amount"},
        {"kind": "column", "table": "Sales", "name": "Amount"},
        {"kind": "measure", "name": "Factor"},
    ])["usages"]
    check([a["match"] for a in usage["Amount"]] == [MATCH_TEXT],
          "a qualified name is only a probable measure", str(usage["Amount"]))
    check([a["match"] for a in usage["Sales[Amount]"]] == [MATCH_BINDING], "...and a certain column")
    check([a["match"] for a in usage["Factor"]] == [MATCH_BINDING], "an unqualified name is a measure")


def test_regression_partial_declarations(root: str) -> None:
    """
    TMDL allows "partial declarations": a table split across files, several
    tables in one file. The loader read one table per file: the second file
    of a table overwrote the first (its measures vanished from every tool),
    and every measure of a file went to its FIRST table. Then such models
    were refused. Now they are read, and every element is read from and
    written to the file that declares it.
    """
    print("\n[46] Partial declarations: a table split across files, two tables in a file")
    project = os.path.join(root, "partial")
    _write_files(project, {
        "P.SemanticModel/definition/tables/Sales.tmdl": (
            "table Sales\n\tmeasure Base = SUM(Sales[Amount])\n\n"
            "\tcolumn Amount\n\t\tdataType: double\n"),
        # Measures of two tables in one file, outside 'tables/'.
        "P.SemanticModel/definition/measures.tmdl": (
            "table Sales\n\tmeasure Twice = [Base] * 2\n\n"
            "table Product\n\tmeasure Items = COUNTROWS(Product)\n\n"
            "\tcolumn Amount\n\t\tdataType: double\n"),
        "P.SemanticModel/definition/tables/Product.tmdl": "table Product\n\tcolumn Code\n\t\tdataType: string\n",
        "P.SemanticModel/definition/functions.tmdl": "function AddTax = (x: NUMERIC) => x * 1.2\n",
        "P.SemanticModel/definition/expressions.tmdl": "expression Server = \"srv\" meta [IsParameterQuery=true]\n",
    })
    loader.clear_model_cache()
    model = loader.load_semantic_model(project)
    sales, product = model.get("Sales", {}), model.get("Product", {})
    check(sorted(m.name for m in sales.get("measures", [])) == ["Base", "Twice"]
          and [m.name for m in product.get("measures", [])] == ["Items"],
          "each measure belongs to its own table, wherever it is declared",
          str({t: [m.name for m in meta["measures"]] for t, meta in model.items()}))
    check(len(sales.get("parts", [])) == 2 and sales.get("file_stem") == "Sales",
          "the table's main declaration is the file named after it", str(sales.get("file_path")))
    check(sorted(product.get("columns", [])) == ["Amount", "Code"], "columns merged across declarations")

    definition = loader.load_definition(project)
    check("AddTax" in definition["functions"] and "Server" in definition["expressions"],
          "DAX functions and shared expressions are loaded", str(sorted(definition)))

    twice = next((e for e in lineage.build_dax_lineage(project)["elements"] if e["name"] == "Twice"), {})
    check(twice.get("measures") == ["Sales[Base]"], "the lineage reads the measure from its own file", str(twice))
    listed = reader_tools.list_tables(project)
    check("declared in 2 files" in listed, "list_tables shows a table declared in two files", listed)
    measure = reader_tools.get_dax_measure(project, "Sales", "Twice")
    check("[Base] * 2" in measure, "get_dax_measure finds a measure in the second file", measure)
    definition_text = reader_tools.read_table_definition(project, "Product")
    check("Items" in definition_text and "Code" in definition_text and "Twice" not in definition_text,
          "read_table_definition shows every declaration of the table, and only those", definition_text)

    writer_tools.set_display_folder(project, "Sales", "Twice", display_folder="KPI")
    measures_file = os.path.join(project, "P.SemanticModel", "definition", "measures.tmdl")
    check("displayFolder: KPI" in io.open(measures_file, encoding="utf-8").read(),
          "a property is written in the file that declares the element")
    writer_tools.create_measure(project, "Sales", "Thrice", "[Base] * 3")
    check("Thrice" in io.open(loader.get_table_file_path(project, "Sales"), encoding="utf-8").read(),
          "a new measure goes into the table's main declaration")
    message = _expect_error(lambda: writer_tools.create_measure(project, "Sales", "Twice", "1"), (tmdl.TmdlError,))
    check("already exists" in message, "a duplicate declared in another file is refused", message)

    ols_roles.set_ols_permission(project, "R", "Product", "amount")
    roles = io.open(os.path.join(project, "P.SemanticModel", "definition", "roles.tmdl"), encoding="utf-8").read()
    check("tablePermission Product" in roles and "columnPermission Amount" in roles,
          "OLS finds a column declared in the table's second file", roles)


def test_regression_diagnostics() -> None:
    """
    1. A tool failure was not logged at all: a bug report had only the
       message the client showed.
    2. `--check` printed 'mcp present' instead of the version.
    3. 'si'/'sì' were accepted as booleans, a leftover of the Italian version.
    """
    print("\n[47] Diagnostics: failure log, installation check, booleans")
    import contextlib

    from mcp_data_platform_toolkit.mcp_common.errors import tool_error_handler

    def expected_failure():
        raise ValueError("table name missing")

    def defect():
        raise RuntimeError("unexpected state")

    for func, wants_trace in ((expected_failure, False), (defect, True)):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            _expect_error(tool_error_handler(func))
        log = stderr.getvalue()
        check(f"tool '{func.__name__}' failed" in log, f"'{func.__name__}': failure logged", log)
        check(("Traceback" in log) == wants_trace,
              f"'{func.__name__}': stack trace {'logged' if wants_trace else 'not logged'}", log)

    import importlib.metadata

    from mcp_data_platform_toolkit import server as server_module
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        server_module.run_check()
    check(f"mcp       {importlib.metadata.version('mcp')}" in stdout.getvalue(),
          "--check shows the installed mcp version", stdout.getvalue()[-400:])

    check(bool(_expect_error(lambda: tmdl.format_property_value("isHidden", "si"), (tmdl.TmdlError,))),
          "'si' is not a boolean")


def main() -> int:
    root = tempfile.mkdtemp(prefix="mcp_selftest_")
    # The description and lineage tools keep the user's files in the settings folder:
    # pointed into the temporary root, the real one is never touched.
    saved_env = {name: os.environ.get(name) for name in ("APPDATA", "XDG_CONFIG_HOME", "MCP_USER_FOLDER")}
    os.environ["APPDATA"] = os.environ["XDG_CONFIG_HOME"] = os.path.join(root, "user_settings")
    os.environ.pop("MCP_USER_FOLDER", None)
    # Asked once for their own rules: the writes below must not stop at the question.
    from mcp_data_platform_toolkit.semantic_model import description_rules
    real_question = description_rules.needs_guidelines_question
    description_rules.needs_guidelines_question = lambda: False
    try:
        project = create_pbip_project(os.path.join(root, "pbip"))
        workspace = create_report_workspace(os.path.join(root, "reports"))

        test_parser(project)
        test_relationships_and_fact(project)
        test_dax_lineage(project)
        test_writing(project)
        test_autodoc(project)
        test_ols_roles(project)
        test_reading(project)
        test_reports(workspace)
        test_output_shaping()
        test_guidelines()
        test_ambiguous_model(root)
        test_line_endings(project)
        test_mcp_contract(project, root)
        test_atomic_write(project)
        test_lossless_reduction(root)
        test_complete_usage_summary(workspace)

        # Regressions of the security and robustness review. Last, because
        # some of them deliberately modify the project files (concurrent
        # write) and create service folders.
        test_regression_corruption_and_injection(project)
        test_regression_paths(project)
        test_regression_concurrent_write(project)
        test_regression_degenerate_input(root)
        test_regression_long_paths(root)
        test_regression_missing_bridge()
        test_regression_no_silent_cut()
        test_regression_certain_vs_probable(workspace)
        test_regression_no_reload(project)

        # Regressions of the lineage review.
        test_regression_tmdl_expression_bodies()
        test_regression_dax_references(root)
        test_regression_measure_name_unique(root)
        test_regression_autodoc_descriptions(root)
        test_regression_report_coverage(root)
        test_regression_relationship_cardinality(root)
        test_regression_table_name_not_a_file_name(root)
        test_regression_bridge_paths(project)
        test_regression_coverage_survives_reduction()
        test_regression_ols_column_permission_forms(root)

        # Regressions of the review that preceded the end-to-end lineage.
        test_regression_fence_and_control_characters(root)
        test_regression_pbip_identifies_its_model(root)
        test_regression_qualified_references_exist(root)
        test_regression_inline_expression_continued()
        test_regression_property_targets(root)
        test_regression_fact_table_override(project)
        test_regression_ols_names_and_race(root)
        test_regression_bridge_robustness(project)
        test_regression_output_shaping_details()
        test_regression_report_binding_and_measures(root)
        test_regression_partial_declarations(root)
        test_regression_diagnostics()
    except Exception:
        traceback.print_exc()
        return 2
    finally:
        description_rules.needs_guidelines_question = real_question
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n{PASSED} checks passed, {len(FAILED)} failed.")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
