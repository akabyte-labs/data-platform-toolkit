"""
The Power Query reader: the M shapes Power BI Desktop writes and the common
hand-written ones, each column followed back to its source, and what stops a
trace reported with its step.
"""

from __future__ import annotations

import pytest

from mcp_data_platform_toolkit.semantic_model.power_query import _parse_query, _Tracer

QUERIES = {
    "Server": '"myserver" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
    "Customers": ('let\n Source = Sql.Database(Server, "Shop"),\n'
                  ' gold_Customer = Source{[Schema="gold",Item="Customer"]}[Data]\nin\n gold_Customer'),
    "fnTable": ('(schema as text, tbl as text) as table => let\n Source = Sql.Database(Server, "Shop"),\n'
                ' T = Source{[Schema=schema, Item=tbl]}[Data]\nin\n T'),
    "S2023": 'let Source = Sql.Database("s", "d"), t = Source{[Schema="gold",Item="Sales2023"]}[Data] in t',
    "S2024": 'let Source = Sql.Database("s", "d"), t = Source{[Schema="gold",Item="Sales2024"]}[Data] in t',
}


@pytest.fixture
def trace():
    tracer = _Tracer({name: _parse_query(name, text) for name, text in QUERIES.items()})

    def run(m: str, column: str):
        return tracer.column(_parse_query("Q", m), column)
    return run


SALES = '''let
    // the source
    Source = Sql.Database(Server, "Shop"),
    gold_Sales = Source{[Schema="gold",Item="Sales"]}[Data],
    #"Renamed Columns" = Table.RenameColumns(gold_Sales,{{"amt", "Amount"}, {"cust_id", "CustomerKey"}}),
    #"Changed Type" = Table.TransformColumnTypes(#"Renamed Columns",{{"Amount", type number}}),
    /* a computed column */
    #"Added Custom" = Table.AddColumn(#"Changed Type", "Gross", each [Amount] * (1 + [Unit Price (EUR)]), type number),
    #"Merged Queries" = Table.NestedJoin(#"Added Custom", {"CustomerKey"}, Customers, {"Id"}, "Customers", JoinKind.LeftOuter),
    #"Expanded Customers" = Table.ExpandTableColumn(#"Merged Queries", "Customers", {"Name"}, {"Customer Name"}),
    #"Added Index" = Table.AddIndexColumn(#"Expanded Customers", "Index", 1, 1, Int64.Type)
in
    #"Added Index"'''


def _db(origins):
    return [(o.schema, o.name, o.column) for o in origins if o.kind == "db"]


def test_renames_computed_columns_merges_and_parameters(trace):
    amount = trace(SALES, "Amount")
    assert _db(amount) == [("gold", "Sales", "amt")]
    assert (amount[0].server, amount[0].database) == ("myserver", "Shop")
    assert amount[0].how == ("renamed from 'amt' in 'Renamed Columns'",)
    assert _db(trace(SALES, "Gross")) == [("gold", "Sales", "amt"), ("gold", "Sales", "Unit Price (EUR)")]
    assert _db(trace(SALES, "Customer Name")) == [("gold", "Customer", "Name")]
    assert _db(trace(SALES, "OrderDate")) == [("gold", "Sales", "OrderDate")]
    assert trace(SALES, "Index")[0].kind == "generated"


def test_native_queries(trace):
    option = trace('let Source = Sql.Database("s", "d", [Query="SELECT a AS A#(lf)FROM gold.x"]) in Source', "A")
    assert (option[0].kind, option[0].query) == ("native", "SELECT a AS A\nFROM gold.x")
    value = trace('let S = Sql.Database("s", "d"), q = Value.NativeQuery(S, "SELECT b FROM gold.y", null, '
                  '[EnableFolding=true]) in q', "b")
    assert (value[0].kind, value[0].query, value[0].database) == ("native", "SELECT b FROM gold.y", "d")
    built = trace('let S = Sql.Database("s", "d"), q = Value.NativeQuery(S, "SELECT * FROM " & T) in q', "x")
    assert built[0].kind == "unresolved" and "built at refresh time" in built[0].reason


def test_functions_appends_groupings_and_databases(trace):
    assert _db(trace('let S = fnTable("gold", "Product"), r = Table.SelectColumns(S, {"Name"}) in r', "Name")) \
        == [("gold", "Product", "Name")]
    assert _db(trace("let S = Table.Combine({S2023, S2024}) in S", "Qty")) == [
        ("gold", "Sales2023", "Qty"), ("gold", "Sales2024", "Qty")]
    group = ('let S = S2023, g = Table.Group(S, {"Region"}, {{"Total", each List.Sum([Amount]), type number}, '
             '{"N", each Table.RowCount(_), Int64.Type}}) in g')
    assert _db(trace(group, "Region")) == [("gold", "Sales2023", "Region")]
    assert _db(trace(group, "Total")) == [("gold", "Sales2023", "Amount")]
    assert trace(group, "N")[0].kind == "generated"
    databases = 'let S = Sql.Databases("srv"), Shop = S{[Name="Shop"]}[Data], T = Shop{[Schema="gold",Item="T"]}[Data] in T'
    origin = trace(databases, "x")[0]
    assert (origin.server, origin.database, origin.schema, origin.name) == ("srv", "Shop", "gold", "T")


def test_other_connectors_and_what_stops_a_trace(trace):
    excel = trace('let S = Excel.Workbook(File.Contents("C:\\\\b.xlsx"), null, true), '
                  'B = S{[Item="Budget",Kind="Sheet"]}[Data], P = Table.PromoteHeaders(B) in P', "Target")
    assert (excel[0].kind, excel[0].connector, excel[0].name) == ("external", "Excel.Workbook", "Budget")
    lake = trace('let S = Lakehouse.Contents(null), N = S{[workspaceId="w"]}[Data], N1 = N{[lakehouseId="l"]}[Data], '
                 'T = N1{[Id="sales",ItemKind="Table"]}[Data] in T', "x")
    assert (lake[0].kind, lake[0].name) == ("external", "sales")
    unpivot = trace('let S = S2023, p = Table.UnpivotOtherColumns(S, {"Id"}, "Attribute", "Value") in p', "Value")
    assert unpivot[0].kind == "unresolved" and "step 'p'" in unpivot[0].reason
    unknown = trace('let S = S2023, p = Table.Magic(S) in p', "x")
    assert "Table.Magic is not interpreted" in unknown[0].reason
    missing = trace("let S = Nowhere in S", "x")
    assert "neither a step nor a query" in missing[0].reason
    static = trace("Table.FromRows({}, type table [Value = text])", "Value")
    assert static[0].kind == "generated" and "the query" in static[0].reason


def test_a_calculation_group_partition_is_not_an_unknown_source(write_files, tmp_path):
    import os

    from mcp_data_platform_toolkit.semantic_model.power_query import model_sources
    root = write_files({
        "M.SemanticModel/definition.pbism": {"version": "4.0"},
        "M.SemanticModel/definition/model.tmdl": "model Model\n",
        "M.SemanticModel/definition/tables/Time.tmdl": (
            "table Time\n\n\tcalculationGroup\n\n\t\tcalculationItem Current = SELECTEDMEASURE()\n\n"
            "\tcolumn Calculation\n\t\tdataType: string\n\t\tsourceColumn: Name\n\n"
            "\tpartition Time = calculationGroup\n\t\tmode: import\n"),
    }, str(tmp_path / "cg"))
    assert [s.kind for s in model_sources(os.path.join(root, "M.SemanticModel"))] == ["calculation_group"]
