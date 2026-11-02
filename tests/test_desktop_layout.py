"""
A model written the way Power BI Desktop serializes a PBIP project: CRLF line
endings, lineage tags, annotations, auto date/time variations and the
LocalDateTable, a fenced measure, a dynamic format string, a KPI, a
calculation group, a Power Query parameter, an RLS role and a culture.

The other tests build minimal files around one defect each. This one checks
that the parser, the loader, the lineage and the writers agree on a complete
file of the shape Desktop produces: no phantom object, no lost object, no
unresolved reference, a one-line edit staying a one-line edit.
"""

from __future__ import annotations

import io
import os

import pytest

from mcp_data_platform_toolkit.reports.report_tools import collect_field_usage
from mcp_data_platform_toolkit.semantic_model import autodoc, reader_tools, writer_tools
from mcp_data_platform_toolkit.semantic_model import dax_lineage as lineage
from mcp_data_platform_toolkit.semantic_model import model_loader as loader
from mcp_data_platform_toolkit.semantic_model import relationships as rels
from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl

SALES = """table Sales
\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000001

\tmeasure 'Total Sales' = SUM(Sales[Amount])
\t\tformatString: \\$#,0.00;(\\$#,0.00);\\$#,0.00
\t\tdisplayFolder: Base
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000002

\t/// Sales of the same period last year
\tmeasure 'Sales LY' =
\t\t\tCALCULATE(
\t\t\t    [Total Sales],
\t\t\t    SAMEPERIODLASTYEAR('Date'[Date])
\t\t\t)
\t\tformatString: #,0
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000003

\tmeasure 'Sales YoY %' = ```
\t\t\tVAR cur = [Total Sales]
\t\t\tVAR prev = [Sales LY]
\t\t\tRETURN DIVIDE(cur - prev, prev)\x20\x20\x20
\t\t\t```
\t\tformatString: 0.0%
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000004

\t\tchangedProperty = FormatString

\tmeasure 'Dynamic Format' = [Total Sales]
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000005
\t\tformatStringDefinition = IF([Total Sales] > 1000000, "#,0,,.0M", "#,0")

\tcolumn Amount
\t\tdataType: decimal
\t\tformatString: \\$#,0.00;(\\$#,0.00);\\$#,0.00
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000006
\t\tsummarizeBy: sum
\t\tsourceColumn: Amount

\t\tannotation SummarizationSetBy = Automatic

\t\tannotation PBI_FormatHint = {"currencyCulture":"en-US"}

\tcolumn CustomerKey
\t\tdataType: int64
\t\tisHidden
\t\tformatString: 0
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000007
\t\tsummarizeBy: none
\t\tsourceColumn: CustomerKey

\t\tchangedProperty = IsHidden

\t\tannotation SummarizationSetBy = Automatic

\tcolumn OrderDate
\t\tdataType: dateTime
\t\tformatString: General Date
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000008
\t\tsummarizeBy: none
\t\tsourceColumn: OrderDate

\t\tvariation Variation
\t\t\tisDefault
\t\t\trelationship: 3c3f0000-0000-4000-8000-000000000001
\t\t\tdefaultHierarchy: LocalDateTable_0a1b.'Date Hierarchy'

\t\tannotation SummarizationSetBy = Automatic

\tcolumn 'Net Amount' = Sales[Amount] * 0.9
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000009
\t\tsummarizeBy: sum

\t\tannotation SummarizationSetBy = Automatic

\tpartition Sales = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Sql.Database(Server, "SalesDb"),
\t\t\t\t    dbo_Sales = Source{[Schema="dbo",Item="Sales"]}[Data]
\t\t\t\tin
\t\t\t\t    dbo_Sales

\tannotation PBI_NavigationStepName = Navigation

\tannotation PBI_ResultType = Table

"""

CUSTOMER = """table Customer
\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000101

\tcolumn CustomerKey
\t\tdataType: int64
\t\tformatString: 0
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000102
\t\tsummarizeBy: none
\t\tsourceColumn: CustomerKey

\t\tannotation SummarizationSetBy = Automatic

\tcolumn 'Customer Name'
\t\tdataType: string
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000103
\t\tsummarizeBy: none
\t\tsourceColumn: Customer Name

\t\tannotation SummarizationSetBy = Automatic

\tpartition Customer = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Sql.Database(Server, "SalesDb"),
\t\t\t\t    dbo_Customer = Source{[Schema="dbo",Item="Customer"]}[Data]
\t\t\t\tin
\t\t\t\t    dbo_Customer

\tannotation PBI_ResultType = Table

"""

DATE = """table Date
\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000201
\tdataCategory: Time

\tcolumn Date
\t\tdataType: dateTime
\t\tisKey
\t\tformatString: Long Date
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000202
\t\tsummarizeBy: none
\t\tsourceColumn: Date

\t\tannotation SummarizationSetBy = Automatic

\t\tannotation UnderlyingDateTimeDataType = Date

\tcolumn Year
\t\tdataType: int64
\t\tformatString: 0
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000203
\t\tsummarizeBy: none
\t\tsourceColumn: Year

\thierarchy 'Calendar Hierarchy'
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000204

\t\tlevel Year
\t\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000205
\t\t\tcolumn: Year

\t\tlevel Date
\t\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000206
\t\t\tcolumn: Date

\tpartition Date = calculated
\t\tmode: import
\t\tsource = CALENDARAUTO()

\tannotation PBI_Id = 0a1b2c3d

"""

LOCAL_DATE = """table LocalDateTable_0a1b
\tisHidden
\tshowAsVariationsOnly
\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000301

\tcolumn Date
\t\tdataType: dateTime
\t\tisHidden
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000302
\t\tdataCategory: PaddedDateTableDates
\t\tsummarizeBy: none
\t\tisNameInferred
\t\tsourceColumn: [Date]

\t\tannotation SummarizationSetBy = User

\tcolumn Year = YEAR([Date])
\t\tdataType: int64
\t\tisHidden
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000303
\t\tdataCategory: Years
\t\tsummarizeBy: none

\t\tannotation SummarizationSetBy = User

\t\tannotation TemplateId = Year

\thierarchy 'Date Hierarchy'
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000304

\t\tlevel Year
\t\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000305
\t\t\tcolumn: Year

\tpartition LocalDateTable_0a1b = calculated
\t\tmode: import
\t\tsource = Calendar(Date(Year(MIN('Sales'[OrderDate])), 1, 1), Date(Year(MAX('Sales'[OrderDate])), 12, 31))

\tannotation __PBI_LocalDateTable = true

"""

TIME_INTELLIGENCE = """table 'Time Intelligence'
\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000401

\tcalculationGroup
\t\tprecedence: 1

\t\tcalculationItem Current = SELECTEDMEASURE()

\t\tcalculationItem YTD = CALCULATE(SELECTEDMEASURE(), DATESYTD('Date'[Date]))

\t\tcalculationItem 'YoY %' =
\t\t\t\tVAR cur = SELECTEDMEASURE()
\t\t\t\tVAR prev = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))
\t\t\t\tRETURN DIVIDE(cur - prev, prev)
\t\t\tformatStringDefinition = "0.0%"

\tcolumn 'Time Calculation'
\t\tdataType: string
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000402
\t\tsummarizeBy: none
\t\tsourceColumn: Name
\t\tsortByColumn: Ordinal

\t\tannotation SummarizationSetBy = Automatic

\tcolumn Ordinal
\t\tdataType: int64
\t\tisHidden
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000403
\t\tsummarizeBy: sum
\t\tsourceColumn: Ordinal

\t\tannotation SummarizationSetBy = Automatic

"""

MEASURES = """table _Measures
\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000501

\tmeasure 'Sales Target' = 1000000
\t\tformatString: #,0
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000502

\tmeasure 'Sales vs Target' = [Total Sales] - [Sales Target]
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000503

\t\tkpi
\t\t\ttargetExpression = [Sales Target]
\t\t\tstatusGraphic: Traffic Light - Single
\t\t\tstatusExpression =
\t\t\t\t\tVAR x = DIVIDE([Total Sales], [Sales Target])
\t\t\t\t\tRETURN IF(x >= 1, 1, -1)

\tcolumn Value
\t\tdataType: string
\t\tisHidden
\t\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000504
\t\tsummarizeBy: none
\t\tsourceColumn: Value

\t\tannotation SummarizationSetBy = Automatic

\tpartition _Measures = m
\t\tmode: import
\t\tsource = Table.FromRows({}, type table [Value = text])

\tannotation PBI_ResultType = Table

"""

FILES = {
    "Contoso.pbip": {"version": "1.0", "artifacts": [{"report": {"path": "Contoso.Report"}}],
                     "settings": {"enableAutoRecovery": True}},
    "Contoso.Report/definition.pbir": {"version": "4.0", "datasetReference": {
        "byPath": {"path": "../Contoso.SemanticModel"}}},
    "Contoso.Report/definition/pages/p1/page.json": {"displayName": "Overview"},
    "Contoso.Report/definition/pages/p1/visuals/v1/visual.json": {"visual": {"visualType": "card", "query": {
        "queryState": {"Values": {"projections": [{"field": {"Measure": {
            "Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Sales YoY %"}},
            "queryRef": "Sales.Sales YoY %"}]}}}}},
    "Contoso.SemanticModel/definition.pbism": {"version": "4.0", "settings": {}},
    "Contoso.SemanticModel/definition/database.tmdl": "database\n\tcompatibilityLevel: 1567\n\n",
    "Contoso.SemanticModel/definition/model.tmdl": (
        "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tsourceQueryCulture: en-US\n\tdataAccessOptions\n\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n"
        "annotation __PBI_TimeIntelligenceEnabled = 1\n\n"
        'annotation PBI_QueryOrder = ["Sales","Customer","Date"]\n\n'
        "ref table Sales\nref table Customer\nref table Date\nref table LocalDateTable_0a1b\n"
        "ref table 'Time Intelligence'\nref table _Measures\n\nref cultureInfo en-US\n\n"),
    "Contoso.SemanticModel/definition/relationships.tmdl": (
        "relationship 3c3f0000-0000-4000-8000-000000000001\n\tjoinOnDateBehavior: datePartOnly\n"
        "\tfromColumn: Sales.OrderDate\n\ttoColumn: LocalDateTable_0a1b.Date\n\n"
        "relationship 8a8b0000-0000-4000-8000-000000000002\n"
        "\tfromColumn: Sales.CustomerKey\n\ttoColumn: Customer.CustomerKey\n\n"
        "relationship 9c9d0000-0000-4000-8000-000000000003\n\tisActive: false\n"
        "\tcrossFilteringBehavior: bothDirections\n\tfromColumn: Sales.OrderDate\n\ttoColumn: Date.Date\n\n"),
    "Contoso.SemanticModel/definition/expressions.tmdl": (
        'expression Server = "sql.contoso.local" meta [IsParameterQuery=true, Type="Text", '
        "IsParameterQueryRequired=true]\n\tlineageTag: 6f1c2b8e-0000-4000-8000-000000000601\n\n"
        "\tannotation PBI_ResultType = Text\n\n"),
    "Contoso.SemanticModel/definition/roles/Readers.tmdl": (
        "role Readers\n\tmodelPermission: read\n\n"
        "\ttablePermission Customer = [Customer Name] <> \"Internal\"\n\n"),
    "Contoso.SemanticModel/definition/cultures/en-US.tmdl": (
        "cultureInfo en-US\n\n\tlinguisticMetadata =\n\t\t\t{\n\t\t\t  \"Version\": \"1.0.0\",\n"
        "\t\t\t  \"Language\": \"en-US\"\n\t\t\t}\n\t\tcontentType: json\n\n"),
    "Contoso.SemanticModel/definition/tables/Sales.tmdl": SALES,
    "Contoso.SemanticModel/definition/tables/Customer.tmdl": CUSTOMER,
    "Contoso.SemanticModel/definition/tables/Date.tmdl": DATE,
    "Contoso.SemanticModel/definition/tables/LocalDateTable_0a1b.tmdl": LOCAL_DATE,
    "Contoso.SemanticModel/definition/tables/Time Intelligence.tmdl": TIME_INTELLIGENCE,
    "Contoso.SemanticModel/definition/tables/_Measures.tmdl": MEASURES,
}


@pytest.fixture
def contoso(write_files, tmp_path):
    root = write_files(FILES, str(tmp_path / "workspace"))
    return os.path.join(root, "Contoso.pbip")


def test_every_table_and_element_is_read(contoso):
    model = loader.load_semantic_model(contoso)
    assert sorted(model) == ["Customer", "Date", "LocalDateTable_0a1b", "Sales", "Time Intelligence", "_Measures"]
    sales = model["Sales"]
    assert sales["columns"] == ["Amount", "CustomerKey", "OrderDate", "Net Amount"]
    assert [m.name for m in sales["measures"]] == ["Total Sales", "Sales LY", "Sales YoY %", "Dynamic Format"]
    assert [p.name for p in sales["partitions"]] == ["Sales"]
    assert [c.name for c in model["Time Intelligence"]["calc_items"]] == ["Current", "YTD", "YoY %"]
    assert model["Date"]["hierarchies"] == {"Calendar Hierarchy": {"Year": "Year", "Date": "Date"}}

    definition = loader.load_definition(contoso)
    assert list(definition["expressions"]) == ["Server"]
    assert list(definition["roles"]) == ["Readers"]
    assert definition["errors"] == []


def test_no_phantom_declaration_in_expression_bodies(contoso):
    _path, lines = loader.read_table_lines(contoso, "Sales")
    names = [(b.keyword, b.name) for b in tmdl.find_all_blocks(lines)]
    # Every declaration is real: nothing comes from the M source, the fenced
    # body or the dynamic format string.
    assert names == [
        ("table", "Sales"), ("measure", "Total Sales"), ("measure", "Sales LY"),
        ("measure", "Sales YoY %"), ("changedProperty", ""), ("measure", "Dynamic Format"),
        ("column", "Amount"), ("annotation", "SummarizationSetBy"), ("annotation", "PBI_FormatHint"),
        ("column", "CustomerKey"), ("changedProperty", ""), ("annotation", "SummarizationSetBy"),
        ("column", "OrderDate"), ("variation", "Variation"), ("annotation", "SummarizationSetBy"),
        ("column", "Net Amount"), ("annotation", "SummarizationSetBy"), ("partition", "Sales"),
        ("annotation", "PBI_NavigationStepName"), ("annotation", "PBI_ResultType"),
    ]


def test_lineage_resolves_every_reference(contoso):
    report = lineage.build_dax_lineage(contoso)
    unresolved = {e["name"]: e["unresolved"] for e in report["elements"] if e["unresolved"]}
    assert unresolved == {}
    by_name = {e["name"]: e for e in report["elements"]}
    assert by_name["Sales YoY %"]["measures"] == ["Sales[Sales LY]", "Sales[Total Sales]"]
    assert by_name["Sales LY"]["columns"] == ["Date[Date]"]
    assert by_name["Dynamic Format"]["measures"] == ["Sales[Total Sales]"]       # not the format string
    assert by_name["Sales vs Target"]["measures"] == ["Sales[Total Sales]", "_Measures[Sales Target]"]
    assert by_name["YoY %"]["selected_measure"] and by_name["YoY %"]["columns"] == ["Date[Date]"]
    assert [e["name"] for e in report["no_dependencies"]] == ["Sales Target"]


def test_relationships_and_roles(contoso):
    relationships = rels.parse_relationships(contoso)
    assert [(r["from_table"], r["to_table"], r["is_active"], r["cross_filtering"]) for r in relationships] == [
        ("Sales", "LocalDateTable_0a1b", True, "singleDirection"),
        ("Sales", "Customer", True, "singleDirection"),
        ("Sales", "Date", False, "bothDirections"),
    ]
    assert rels.is_fact_table(contoso, "Sales") and not rels.is_fact_table(contoso, "Customer")
    ols = reader_tools.get_ols_permissions(contoso)
    assert "Readers" in ols and "RLS filter" in ols


def test_a_one_line_edit_stays_a_one_line_edit(contoso):
    path = loader.get_table_file_path(contoso, "Sales")
    with io.open(path, encoding="utf-8", newline="") as handle:
        before = handle.read().split("\r\n")

    writer_tools.set_display_folder(contoso, "Sales", "Sales LY", display_folder="Time")

    with io.open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    after = text.split("\r\n")
    assert "\n" not in text.replace("\r\n", "")                                 # CRLF everywhere
    assert len(after) == len(before) + 1
    added = [line for line in after if line not in before]
    assert added == ["\t\tdisplayFolder: Time"]
    index = after.index("\t\tdisplayFolder: Time")
    assert after[:index] + after[index + 1:] == before


def test_readers_on_desktop_shapes(contoso):
    fenced = reader_tools.get_dax_measure(contoso, "Sales", "Sales YoY %")
    assert "RETURN DIVIDE(cur - prev, prev)" in fenced and "formatString: 0.0%" in fenced
    state = autodoc.gather_autodoc_state(contoso, "Sales", "Sales LY", "measure", "")
    assert state["existing_texts"] == ["Sales of the same period last year"]
    usage = collect_field_usage(os.path.dirname(contoso), [{"kind": "measure", "name": "Sales YoY %"}])
    assert [u["page"] for u in usage["usages"]["Sales YoY %"]] == ["Overview"]
