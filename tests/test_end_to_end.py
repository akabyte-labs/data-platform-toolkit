"""
End-to-end lineage: a database project, the semantic model that reads it
(Power Query with renames and computed columns, a native query, a silver
view, an Excel file, an unpivot), its measures and field parameter, and a
PBIR report with visuals, a page filter, a bookmark and a report-level
measure.
"""

from __future__ import annotations

import os

import pytest

from mcp_data_platform_toolkit.lineage import lineage_tools
from mcp_data_platform_toolkit.lineage.end_to_end import build_end_to_end
from mcp_data_platform_toolkit.semantic_model import power_query
from tests.test_sql_lineage import CONFIG, PROJECT


def _name(name: str) -> str:
    return f"'{name}'" if not name.replace("_", "").isalnum() else name


def _table(name: str, columns=(), measures=(), partitions=()) -> str:
    """A table as Power BI Desktop writes it (tabs, the M indented two levels below 'source =')."""
    lines = [f"table {_name(name)}", "\tlineageTag: 00000000-0000-4000-8000-000000000000", ""]
    for measure, expression in measures:
        lines += [f"\tmeasure {_name(measure)} = {expression}", "", ""]
    for column in columns:
        column_name, _sep, rest = column.partition("=")
        column_name = column_name.strip()
        if rest:
            lines += [f"\tcolumn {_name(column_name)} = {rest.strip()}", "\t\tsummarizeBy: none", ""]
        else:
            source = column_name
            if "<-" in column_name:
                column_name, source = (p.strip() for p in column_name.split("<-"))
            lines += [f"\tcolumn {_name(column_name)}", "\t\tdataType: string",
                      f"\t\tsourceColumn: {source}", ""]
    for partition, kind, source in partitions:
        lines += [f"\tpartition {_name(partition)} = {kind}", "\t\tmode: import", "\t\tsource ="]
        lines += ["\t\t\t\t" + line for line in source.strip().splitlines()]
        lines.append("")
    return "\n".join(lines) + "\n"


def _m(schema: str, item: str, steps: str = "", last: str = "") -> str:
    navigation = f"{schema}_{item}"
    body = ["let", '    Source = Sql.Database(Server, "Shop"),',
            f'    {navigation} = Source{{[Schema="{schema}",Item="{item}"]}}[Data]' + ("," if steps else "")]
    if steps:
        body += ["    " + line for line in steps.strip().splitlines()]
    body += ["in", f"    {last or navigation}"]
    return "\n".join(body)


def _field(entity: str, prop: str, kind: str = "Column") -> dict:
    return {"field": {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}}


def _visual(visual_type: str, fields: list[dict], title: str = "") -> dict:
    visual = {"visualType": visual_type, "query": {"queryState": {"Values": {"projections": fields}}}}
    if title:
        visual["visualContainerObjects"] = {"title": [{"properties": {"text": {
            "expr": {"Literal": {"Value": f"'{title}'"}}}}}]}
    return {"visual": visual}


MODEL = "bi/Shop.SemanticModel/definition"
REPORT = "bi/Shop.Report/definition"

FILES = {
    **{f"db/{k}": v for k, v in PROJECT.items()},
    "db/lineage.yaml": CONFIG,
    "bi/Shop.pbip": {"version": "1.0", "artifacts": [{"report": {"path": "Shop.Report"}}]},
    "bi/Shop.SemanticModel/definition.pbism": {"version": "4.0", "settings": {}},
    f"{MODEL}/database.tmdl": "database\n\tcompatibilityLevel: 1567\n",
    f"{MODEL}/model.tmdl": "model Model\n\tculture: en-US\n",
    f"{MODEL}/expressions.tmdl": ('expression Server = "sql.shop.local" meta [IsParameterQuery=true, '
                                  'Type="Text", IsParameterQueryRequired=true]\n'),
    f"{MODEL}/tables/Sales.tmdl": _table(
        "Sales",
        columns=["OrderId", "Sales Amount", "CustomerName", "Market", "Amount x2",
                 'Big = IF(Sales[Sales Amount] > 100, "Y", "N")'],
        partitions=[("Sales", "m", _m("mart", "sales", (
            '#"Renamed Columns" = Table.RenameColumns(mart_sales,{{"Amount", "Sales Amount"}}),\n'
            '#"Added Custom" = Table.AddColumn(#"Renamed Columns", "Amount x2", each [Sales Amount] * 2)'),
            '#"Added Custom"'))]),
    f"{MODEL}/tables/Targets.tmdl": _table(
        "Targets", columns=["Year", "Stretch"],
        partitions=[("Targets", "m", 'let\n    Source = Sql.Database(Server, "Shop"),\n'
                     '    Query = Value.NativeQuery(Source, "SELECT t.Year, t.Target * 1.1 AS Stretch'
                     '#(lf)FROM mart.targets t", null, [EnableFolding=true])\nin\n    Query')]),
    f"{MODEL}/tables/Customers.tmdl": _table(
        "Customers", columns=["CustomerKey", "CustomerName"],
        partitions=[("Customers", "m", _m("core", "customer"))]),
    f"{MODEL}/tables/Budget.tmdl": _table(
        "Budget", columns=["Region", "Budget"],
        partitions=[("Budget", "m", 'let\n    Source = Excel.Workbook(File.Contents("C:\\data\\budget.xlsx"), '
                     'null, true),\n    Budget_Sheet = Source{[Item="Budget",Kind="Sheet"]}[Data],\n'
                     '    #"Promoted Headers" = Table.PromoteHeaders(Budget_Sheet)\nin\n'
                     '    #"Promoted Headers"')]),
    f"{MODEL}/tables/Pivoted.tmdl": _table(
        "Pivoted", columns=["Attribute", "Value"],
        partitions=[("Pivoted", "m", _m("mart", "sales", (
            'Unpivoted = Table.UnpivotOtherColumns(mart_sales, {"OrderId"}, "Attribute", "Value")'),
            "Unpivoted"))]),
    f"{MODEL}/tables/_Measures.tmdl": _table(
        "_Measures",
        measures=[("Total Sales", "SUM(Sales[Sales Amount])"),
                  ("Stretch Total", "SUM(Targets[Stretch])"),
                  ("Ratio", "DIVIDE([Total Sales], [Stretch Total])")],
        columns=["Value"],
        partitions=[("_Measures", "m", "Table.FromRows({}, type table [Value = text])")]),
    f"{MODEL}/tables/Metric.tmdl": _table(
        "Metric", columns=["Metric <- [Value1]", "Metric Fields <- [Value2]", "Metric Order <- [Value3]"],
        partitions=[("Metric", "calculated", '{\n    ("Sales", NAMEOF(\'_Measures\'[Total Sales]), 0),\n'
                     '    ("Stretch", NAMEOF(\'_Measures\'[Stretch Total]), 1)\n}')]),
    "bi/Shop.Report/definition.pbir": {"version": "4.0", "datasetReference": {
        "byPath": {"path": "../Shop.SemanticModel"}}},
    f"{REPORT}/pages/p1/page.json": {"displayName": "Overview", "filterConfig": {"filters": [
        _field("Sales", "Market")]}},
    f"{REPORT}/pages/p1/visuals/v1/visual.json": _visual("card", [_field("_Measures", "Ratio", "Measure")], "Ratio"),
    f"{REPORT}/pages/p1/visuals/v2/visual.json": _visual(
        "tableEx", [_field("Sales", "CustomerName"), _field("_Measures", "Total Sales", "Measure")], "Detail"),
    f"{REPORT}/pages/p1/visuals/v3/visual.json": _visual("slicer", [_field("Metric", "Metric")]),
    f"{REPORT}/pages/p1/visuals/v4/visual.json": _visual("card", [_field("Sales", "Double", "Measure")], "Double"),
    f"{REPORT}/pages/p1/visuals/v5/visual.json": _visual("card", [_field("Budget", "Budget")], "Budget"),
    f"{REPORT}/reportExtensions.json": {"name": "extension", "entities": [
        {"name": "Sales", "measures": [{"name": "Double", "expression": "[Total Sales] * 2"}]}]},
    f"{REPORT}/bookmarks/b1.bookmark.json": {"explorationState": {"filters": {"byExpr": [
        {"expression": {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Market"}}}]}}},
}


@pytest.fixture
def shop(write_files, tmp_path):
    root = write_files(FILES, str(tmp_path / "ws"))
    return os.path.join(root, "bi", "Shop.pbip"), os.path.join(root, "db")


def _sources(graph, label: str) -> set[str]:
    target = next(k for k, n in graph.nodes.items() if n.label == label)
    return {graph.nodes[e.source].label for e in graph.edges if e.target == target}


def _upstream(graph, label: str) -> set[str]:
    root = next(k for k, n in graph.nodes.items() if n.label == label)
    return {n.label for n in graph.select([root], "upstream").nodes.values()}


def _downstream(graph, label: str) -> set[str]:
    root = next(k for k, n in graph.nodes.items() if n.label == label)
    return {n.label for n in graph.select([root], "downstream").nodes.values()}


# ---------------------------------------------------------------------------
# Power Query
# ---------------------------------------------------------------------------

def test_power_query_renames_and_computed_columns_reach_the_view(shop):
    model, db = shop
    e2e = build_end_to_end(model, db)
    assert _sources(e2e.columns, "Sales[Sales Amount]") == {"mart.sales.Amount"}
    assert _sources(e2e.columns, "Sales[Amount x2]") == {"mart.sales.Amount"}
    edge = next(e for e in e2e.columns.edges if e2e.columns.nodes[e.target].label == "Sales[Amount x2]")
    assert edge.evidence == ("partition 'Sales': renamed from 'Amount' in 'Renamed Columns'; "
                             "computed in 'Added Custom'")
    assert _sources(e2e.columns, "Sales[Big]") == {"Sales[Sales Amount]"}


def test_native_query_columns_are_followed_into_the_database(shop):
    model, db = shop
    e2e = build_end_to_end(model, db)
    assert _sources(e2e.columns, "Targets[Stretch]") == {"mart.targets.Target"}
    assert _sources(e2e.columns, "Targets[Year]") == {"mart.targets.Year"}
    assert "file:targets/*.csv (Target)" in _upstream(e2e.columns, "Targets[Stretch]")


def test_other_sources_and_what_is_not_traced(shop):
    model, db = shop
    e2e = build_end_to_end(model, db)
    assert _sources(e2e.columns, "Budget[Budget]") == {"Budget.Budget (Excel.Workbook)"}
    assert e2e.columns.nodes["model:pivoted[value]"].flags == ["not traced"]
    assert any("Table.UnpivotOtherColumns reshapes the table" in p for p in e2e.model.problems["not_traced"])
    assert e2e.columns.nodes["model:_measures[value]"].flags == ["generated in Power Query"]


def test_direct_lake_and_parameters(write_files, tmp_path):
    root = write_files({
        "M.SemanticModel/definition.pbism": {"version": "4.0"},
        "M.SemanticModel/definition/model.tmdl": "model Model\n",
        "M.SemanticModel/definition/tables/Sales.tmdl": (
            "table Sales\n\n\tcolumn Amount\n\t\tdataType: double\n\t\tsourceColumn: amount\n\n"
            "\tpartition Sales = entity\n\t\tmode: directLake\n\t\tsource\n\t\t\tentityName: fact_sales\n"
            "\t\t\tschemaName: gold\n\t\t\texpressionSource: DatabaseQuery\n"),
    }, str(tmp_path / "dl"))
    source = power_query.model_sources(os.path.join(root, "M.SemanticModel"))[0]
    assert source.kind == "direct_lake"
    origin = source.columns["Amount"][0]
    assert (origin.schema, origin.name, origin.column, origin.connector) == ("gold", "fact_sales", "amount",
                                                                             "Direct Lake")


# ---------------------------------------------------------------------------
# Model and reports
# ---------------------------------------------------------------------------

def test_from_a_visual_back_to_the_source_columns(shop):
    model, db = shop
    e2e = build_end_to_end(model, db)
    behind = _upstream(e2e.columns, "Shop / Overview / 'Ratio' (card)")
    assert {"[Ratio]", "[Total Sales]", "[Stretch Total]", "Sales[Sales Amount]", "mart.sales.Amount",
            "core.sales.Amount", "raw.orders.Qty", "raw.orders.Price", "file:sales/orders/ (Qty)",
            "Targets[Stretch]", "mart.targets.Target", "file:targets/*.csv (Target)"} <= behind


def test_impact_of_a_source_column_on_measures_and_visuals(shop):
    model, db = shop
    e2e = build_end_to_end(model, db)
    impacted = _downstream(e2e.columns, "raw.orders.Qty")
    assert {"[Total Sales]", "[Ratio]", "Shop / Overview / 'Ratio' (card)",
            "Shop / Overview / 'Detail' (tableEx)", "Shop / [Double]",
            "Shop / Overview / 'Double' (card)", "Metric[Metric]", "Shop / Overview / slicer v3"} <= impacted
    assert "Shop / Overview / 'Budget' (card)" not in impacted


def test_filters_bookmarks_and_field_parameters(shop):
    model, db = shop
    e2e = build_end_to_end(model, db)
    assert _sources(e2e.columns, "Shop / Overview / (filters)") == {"Sales[Market]"}
    assert _sources(e2e.columns, "Shop / bookmark 'b1'") == {"Sales[Market]"}
    assert _sources(e2e.columns, "Metric[Metric]") == {"[Total Sales]", "[Stretch Total]"}
    assert _sources(e2e.columns, "Shop / [Double]") == {"[Total Sales]"}


def test_checks_of_the_model_side(shop):
    model, db = shop
    e2e = build_end_to_end(model, db)
    assert e2e.checks["reads_other_layers"] == ["Customers <- core.customer [silver]"]
    assert e2e.checks["databases"] == [("sql.shop.local", "Shop")]
    traced, total = e2e.checks["data_columns"]
    assert (traced, total) == (total - 2, total)          # the unpivoted columns
    assert e2e.checks["visuals"] == (5, 5)


def test_object_level_overview(shop):
    model, db = shop
    e2e = build_end_to_end(model, db)
    edges = {(e2e.objects.nodes[e.source].label, e2e.objects.nodes[e.target].label) for e in e2e.objects.edges}
    assert ("mart.sales", "Sales") in edges and ("core.sales", "mart.sales") in edges
    assert ("[Ratio]", "Shop / Overview") in edges
    assert ("Sales", "[Total Sales]") in edges


def test_without_the_database_project(shop):
    model, _db = shop
    e2e = build_end_to_end(model)
    node = e2e.columns.nodes["mart.sales|amount"]
    assert (node.label, node.layer) == ("mart.sales.Amount", "database")
    assert _sources(e2e.columns, "Targets[Stretch]") == {"mart.targets.Target"}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def test_tool_upstream_tree_from_a_visual(shop):
    model, db = shop
    text = lineage_tools.get_end_to_end_lineage(model, target="Shop/Overview/Ratio", direction="upstream",
                                                sql_project_path=db, output_format="tree")
    assert text.startswith("=== END-TO-END LINEAGE: Shop (column level) ===")
    assert "Shop / Overview / 'Ratio' (card) [report]\n   <- [Ratio] [semantic model]  binding" in text
    assert "<- raw.orders.Qty [bronze]  merge" in text
    assert "Coverage:" in text and "Warnings: 1 model sources outside the 'gold' layer" in text


def test_tool_targets(shop):
    model, db = shop
    downstream = lineage_tools.get_end_to_end_lineage(model, target="mart.sales.Amount",
                                                      direction="downstream", sql_project_path=db)
    assert "Sales[Sales Amount] [semantic model] -> [Total Sales] [semantic model]  dax" in downstream
    measure = lineage_tools.get_end_to_end_lineage(model, target="[Total Sales]", direction="downstream",
                                                   sql_project_path=db)
    assert "[Total Sales] [semantic model] -> Shop / [Double] [report]  report measure" in measure
    table = lineage_tools.get_end_to_end_lineage(model, target="Sales", level="object", direction="upstream",
                                                 sql_project_path=db)
    assert "mart.sales [gold] -> Sales [semantic model]  power query" in table
    with pytest.raises(LookupError, match="neither a column nor a measure"):
        lineage_tools.get_end_to_end_lineage(model, target="Sales[Nope]")
    with pytest.raises(LookupError, match="No report, page or visual"):
        lineage_tools.get_end_to_end_lineage(model, target="Shop/Nowhere")


def test_check_tool_with_the_model(shop):
    model, db = shop
    report = lineage_tools.test_lineage_config(db, project_path=model)
    assert "Semantic model: Shop" in report
    assert "[!] Model tables reading from a layer other than 'gold': Customers <- core.customer [silver]" in report
    assert "[!] Model columns whose source is not traced (2)" in report
    assert "Databases read: Shop on sql.shop.local" in report
