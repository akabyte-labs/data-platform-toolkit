"""
The read tools that answer without the whole TMDL: the model search, the
table summary, Row-Level Security (also in the impact analysis),
perspectives and translations.

They run on the model written the way Power BI Desktop serializes it
(`test_desktop_layout`), plus the roles, perspectives and cultures each
test needs.
"""

from __future__ import annotations

import os

import pytest

from mcp_data_platform_toolkit.reports.report_tools import collect_field_usage, find_measure_usage_in_reports
from mcp_data_platform_toolkit.semantic_model import model_search, reader_tools, rls
from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.tmdl_parser import ElementNotFoundError
from tests.test_desktop_layout import FILES

MODEL = "Contoso.SemanticModel/definition"


@pytest.fixture
def make_project(write_files, tmp_path):
    """The Desktop-shaped project with extra or replaced files ({path: text}, None removes)."""
    def make(extra: dict | None = None) -> str:
        files = dict(FILES)
        for path, content in (extra or {}).items():
            if content is None:
                files.pop(path, None)
            else:
                files[path] = content
        root = write_files(files, str(tmp_path / "workspace"))
        return os.path.join(root, "Contoso.pbip")
    return make


@pytest.fixture
def contoso(make_project):
    return make_project()


# ---------------------------------------------------------------------------
# search_model
# ---------------------------------------------------------------------------

def _labels(project: str, query: str, **kwargs) -> list[str]:
    return [m["label"] for m in model_search.search_model(project, query, **kwargs)["matches"]]


def test_search_finds_names_dax_m_and_descriptions(contoso):
    # DAX of measures, the dynamic format string included, and the name itself.
    assert _labels(contoso, "total sales", object_types=["measure"]) == [
        "_Measures[Sales vs Target]", "Sales[Dynamic Format]", "Sales[Sales LY]",
        "Sales[Sales YoY %]", "Sales[Total Sales]"]
    # M of the partitions, never read by the DAX lineage.
    assert _labels(contoso, "Sql.Database") == ["Customer / partition 'Customer'",
                                                "Sales / partition 'Sales'"]
    # The sourceColumn of a data column, the DAX of a calculated one.
    assert "Customer[Customer Name]" in _labels(contoso, "customer name", search_in=["expression"])
    assert _labels(contoso, "* 0.9") == ["Sales[Net Amount]"]
    # '///' descriptions, and the RLS filter of a role.
    assert _labels(contoso, "same period", search_in=["description"]) == ["Sales[Sales LY]"]
    assert _labels(contoso, "internal") == ["role 'Readers' on Customer"]
    # Shared expressions (parameters).
    assert _labels(contoso, "contoso.local") == ["Server"]


def test_search_says_where_it_matched(contoso):
    text = reader_tools.search_model(contoso, "SAMEPERIODLASTYEAR")
    assert text.startswith("=== SEARCH 'SAMEPERIODLASTYEAR' in name, expression, description: 2 matches")
    assert "measure: Sales[Sales LY]  -- DAX, line 3: SAMEPERIODLASTYEAR('Date'[Date])" in text
    assert "calculation item: 'Time Intelligence'[YoY %]  -- DAX, line 2:" in text
    hidden = reader_tools.search_model(contoso, "CustomerKey", object_types=["column"])
    assert "column: Sales[CustomerKey] [hidden]" in hidden


def test_search_filters_and_regex(contoso):
    assert _labels(contoso, "hierarchy", object_types=["hierarchies"]) == [
        "Date[Calendar Hierarchy]", "LocalDateTable_0a1b[Date Hierarchy]"]
    assert _labels(contoso, "hierarchy", object_types="hierarchy", table_name="date") == [
        "Date[Calendar Hierarchy]"]
    assert _labels(contoso, r"^Sales (LY|Target)$", search_in="name", regex=True) == [
        "_Measures[Sales Target]", "Sales[Sales LY]"]
    assert reader_tools.search_model(contoso, "no such text") == \
        "No match for 'no such text' in name, expression, description."


@pytest.mark.parametrize("kwargs, message", [
    ({"query": "  "}, "cannot be empty"),
    ({"query": "x", "regex": True, "search_in": ["nowhere"]}, "Invalid search_in"),
    ({"query": "x", "object_types": ["visual"]}, "Invalid object type"),
    ({"query": "(", "regex": True}, "Invalid regular expression"),
    ({"query": "x", "table_name": "Nope"}, "not found"),
])
def test_search_rejects_bad_arguments(contoso, kwargs, message):
    with pytest.raises((ValueError, FileNotFoundError), match=message):
        model_search.search_model(contoso, **kwargs)


# ---------------------------------------------------------------------------
# get_table_summary
# ---------------------------------------------------------------------------

def test_table_summary_lists_everything_but_the_code(contoso):
    text = reader_tools.get_table_summary(contoso, "sales")
    assert text.splitlines()[0] == \
        "=== TABLE Sales: 4 columns, 4 measures, 0 hierarchies, 3 relationships ==="
    assert "Kind: fact table (many side of 3 relationships)" in text
    assert "  Sales: m, import | reads: dbo.Sales" in text
    assert "  CustomerKey: int64 | hidden | format: 0" in text
    assert "  Net Amount: calculated" in text
    assert "  Total Sales: folder: Base | format: \\$#,0.00;(\\$#,0.00);\\$#,0.00" in text
    assert "  Dynamic Format: format: dynamic (formatStringDefinition)" in text
    assert "  Sales[CustomerKey] *--1 Customer[CustomerKey] | active | singleDirection" in text
    assert "Documented ('///'): 1 of 8 visible elements" in text
    assert "SUM(" not in text and "Sql.Database" not in text          # no DAX, no M
    assert "same period" not in text
    detailed = reader_tools.get_table_summary(contoso, "Sales", include_descriptions=True)
    assert '  Sales LY: format: #,0  -- "Sales of the same period last year"' in detailed


def test_table_summary_of_other_kinds(contoso):
    date = reader_tools.get_table_summary(contoso, "Date")
    assert "Kind: calculated table, dataCategory: Time" in date
    assert "  Calendar Hierarchy: Year > Date" in date
    assert "  Date: dateTime | key | format: Long Date" in date
    group = reader_tools.get_table_summary(contoso, "Time Intelligence")
    assert "3 calculation items" in group.splitlines()[0]
    assert "Kind: calculation group" in group
    assert "  YoY %: formatStringDefinition" in group
    assert "  Time Calculation: string | sortBy: Ordinal" in group
    assert "Kind: calculated table, hidden" in reader_tools.get_table_summary(contoso, "LocalDateTable_0a1b")
    with pytest.raises(FileNotFoundError):
        reader_tools.get_table_summary(contoso, "Nope")


# ---------------------------------------------------------------------------
# Row-Level Security
# ---------------------------------------------------------------------------

ROLES = {
    f"{MODEL}/roles/Readers.tmdl": None,
    f"{MODEL}/roles/Regional.tmdl": (
        "role Regional\n\tmodelPermission: read\n\n"
        "\t/// Each manager sees the customers of their region\n"
        "\ttablePermission Customer =\n"
        "\t\t\tVAR me = USERPRINCIPALNAME()\n"
        "\t\t\tRETURN [Customer Name] = me && 'Customer'[Nope] = 1\n\n"
        "\ttablePermission Date\n\t\tmetadataPermission: none\n\n"
        "\tmember 'manager@contoso.com'\n\tmember 'sales@contoso.com' = group\n"),
    f"{MODEL}/roles/Nobody.tmdl": "role Nobody\n\tmodelPermission: read\n\n\ttablePermission Sales = FALSE()\n",
    f"{MODEL}/roles/Open.tmdl": "role Open\n\tmodelPermission: read\n",
}


def test_rls_filters_with_dependencies_and_reach(make_project):
    project = make_project(ROLES)
    text = reader_tools.get_rls_filters(project)
    assert text.startswith("=== ROW-LEVEL SECURITY (3 roles, 2 filters) ===")
    assert "Role: Open  (modelPermission: read): no RLS filter, every row is visible" in text
    assert "  Sales [hides every row]: FALSE()" in text
    assert "Role: Regional  (modelPermission: read, 2 members in the file)" in text
    assert "  Customer [dynamic: USERPRINCIPALNAME]:" in text
    assert "      RETURN [Customer Name] = me && 'Customer'[Nope] = 1" in text
    assert "    reads: Customer[Customer Name]" in text
    assert "[!] unresolved: Customer[Nope] (no such column or measure in 'Customer')" in text
    # Customer filters Sales (one -> many); Sales filters nothing.
    assert "    also restricts (relationships): Sales" in text
    assert "(OLS only, no row filter, on: Date: see get_ols_permissions)" in text


def test_rls_reach_follows_only_active_relationships_and_security_direction(make_project):
    relationships = FILES[f"{MODEL}/relationships.tmdl"]
    roles = {**ROLES, f"{MODEL}/roles/Dates.tmdl":
             "role Dates\n\ttablePermission Date = YEAR('Date'[Date]) >= 2020\n"}
    # The Sales -> Date relationship is inactive: a filter on Date reaches nothing.
    project = make_project(roles)
    item = next(f for r in rls.load_role_security(project) if r.name == "Dates" for f in r.filters)
    assert item.restricts == []
    # Active, but security filtering one way: Sales is restricted, Customer is not.
    both = relationships.replace("\tisActive: false\n", "")
    project = make_project({**roles, f"{MODEL}/relationships.tmdl": both})
    item = next(f for r in rls.load_role_security(project) if r.name == "Dates" for f in r.filters)
    assert item.restricts == ["Sales"]
    # Security filter in both directions on Sales -> Customer: Customer too.
    both = both.replace("\tfromColumn: Sales.CustomerKey\n",
                        "\tsecurityFilteringBehavior: bothDirections\n\tfromColumn: Sales.CustomerKey\n")
    project = make_project({**roles, f"{MODEL}/relationships.tmdl": both})
    item = next(f for r in rls.load_role_security(project) if r.name == "Dates" for f in r.filters)
    assert item.restricts == ["Customer", "Sales"]


def test_rls_filters_by_role_and_table(make_project):
    project = make_project(ROLES)
    sales = reader_tools.get_rls_filters(project, table_name="Sales")
    assert "Role: Nobody" in sales and "Role: Regional" in sales and "Role: Open" not in sales
    assert "  Customer [dynamic: USERPRINCIPALNAME; reaches 'Sales' through relationships]:" in sales
    only = reader_tools.get_rls_filters(project, role_name="nobody")
    assert "Role: Regional" not in only and "FALSE()" in only
    assert reader_tools.get_rls_filters(project, table_name="Date") == \
        "No RLS filter is on table 'Date' or reaches it through relationships."
    with pytest.raises(ElementNotFoundError):
        reader_tools.get_rls_filters(project, role_name="Missing")


def test_no_roles(make_project):
    project = make_project({f"{MODEL}/roles/Readers.tmdl": None})
    assert reader_tools.get_rls_filters(project) == "No security roles defined in the model."


def test_impact_analysis_counts_the_columns_a_role_reads(contoso):
    workspace = os.path.dirname(contoso)
    usage = collect_field_usage(workspace, [{"kind": "column", "table": "Customer", "name": "customer name"},
                                            {"kind": "column", "table": "", "name": "Amount"}])
    assert usage["rls_checked"] is True
    assert usage["rls"] == {"Customer[customer name]": ["role 'Readers', filter on 'Customer' (model Contoso)"],
                            "Amount": []}
    text = find_measure_usage_in_reports(workspace, column_names=["Customer[Customer Name]"])
    assert ("'Customer[Customer Name]': NO usages found; read by RLS: role 'Readers', "
            "filter on 'Customer' (model Contoso)") in text


def test_impact_analysis_says_when_no_model_was_there_to_check(write_files, tmp_path):
    report = {k: v for k, v in FILES.items() if k.startswith("Contoso.Report/")}
    report["Contoso.Report/definition.pbir"] = {"version": "4.0", "datasetReference": {"byConnection": {
        "connectionString": "Data Source=powerbi://api.powerbi.com/v1.0/myorg/WS;initial catalog=Contoso"}}}
    workspace = write_files(report, str(tmp_path / "reports"))
    text = find_measure_usage_in_reports(workspace, measure_name="Sales YoY %")
    assert "RLS filters not checked: no local semantic model behind these reports" in text


# ---------------------------------------------------------------------------
# Perspectives
# ---------------------------------------------------------------------------

PERSPECTIVES = {
    f"{MODEL}/perspectives/Sales View.tmdl": (
        "perspective 'Sales View'\n"
        "\tperspectiveTable Sales\n"
        "\t\tperspectiveMeasure 'total sales'\n"
        "\t\tperspectiveMeasure 'Old Measure'\n"
        "\t\tperspectiveColumn Amount\n"
        "\tperspectiveTable Customer\n"
        "\t\tincludeAll\n"
        "\tperspectiveTable Gone\n"
        "\t\tperspectiveColumn X\n"),
    f"{MODEL}/perspectives/Dates.tmdl": (
        "perspective Dates\n\tperspectiveTable Date\n\t\tperspectiveHierarchy 'Calendar Hierarchy'\n"),
}


def test_perspectives_with_members_and_stale_references(make_project):
    project = make_project(PERSPECTIVES)
    text = reader_tools.get_perspectives(project)
    assert text.startswith("=== PERSPECTIVES (2) ===")
    assert "Perspective: Dates  (1 table; 1 hierarchy)" in text
    assert "Perspective: Sales View  (3 tables; 2 measures, 2 columns, 1 whole table)" in text
    # Members are reported as the model declares them.
    assert "  Sales: measures: Total Sales, Old Measure | columns: Amount" in text
    assert "  Customer: the whole table (includeAll)" in text
    assert "[!] not in the model (references that don't resolve): Sales[Old Measure] (measure), Gone (table)" in text
    assert "Tables in no perspective: _Measures, LocalDateTable_0a1b, Time Intelligence" in text
    one = reader_tools.get_perspectives(project, "dates")
    assert "Sales View" not in one and "Tables in no perspective" not in one
    with pytest.raises(ElementNotFoundError, match="perspectives: Dates, Sales View"):
        reader_tools.get_perspectives(project, "Nope")


def test_no_perspectives(contoso):
    assert reader_tools.get_perspectives(contoso) == "No perspectives defined in the model."


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------

GB = (
    "cultureInfo en-GB\n\n"
    "\ttranslations\n\n"
    "\t\tmodel Model\n"
    "\t\t\tcaption: Data model\n\n"
    "\t\t\ttable Sales\n"
    "\t\t\t\tcaption: Turnover\n"
    "\t\t\t\tdescription: The turnover\n\n"
    "\t\t\t\tmeasure 'Total Sales'\n"
    "\t\t\t\t\tcaption: Total turnover\n"
    "\t\t\t\t\tdisplayFolder: Base\n\n"
    "\t\t\t\tcolumn Amount\n"
    "\t\t\t\t\tcaption: Value\n\n"
    "\t\t\t\tcolumn 'Old Column'\n"
    "\t\t\t\t\tcaption: Former\n\n"
    "\t\t\ttable Customer\n"
    "\t\t\t\tcaption: Client\n\n"
    "\t\t\ttable Gone\n"
    "\t\t\t\tcaption: Removed\n\n"
    "\tlinguisticMetadata =\n\t\t\t{\n\t\t\t  \"Version\": \"1.0.0\"\n\t\t\t}\n\t\tcontentType: json\n"
)


def test_culture_blocks_are_parsed():
    lines = [line + "\n" for line in GB.split("\n")]
    blocks = [(b.keyword, b.name) for b in tmdl.find_all_blocks(lines)]
    assert blocks[:3] == [("cultureInfo", "en-GB"), ("translations", ""), ("model", "Model")]
    assert ("linguisticMetadata", "") in blocks


def test_translation_summary_per_culture(make_project):
    project = make_project({f"{MODEL}/cultures/en-GB.tmdl": GB})
    text = reader_tools.get_translations(project)
    assert text.startswith("=== TRANSLATIONS (2 cultures; model culture en-US) ===")
    assert "  en-US: no translations (linguistic metadata only)" in text
    # Visible: 5 tables (LocalDateTable is hidden) and their 8 visible
    # columns, 6 measures and 1 hierarchy.
    assert ("  en-GB: captions for 4 of 20 visible objects (20%), 1 description, 1 display folder; "
            "2 for objects not in the model") in text


def test_translation_detail_of_one_culture(make_project):
    project = make_project({f"{MODEL}/cultures/en-GB.tmdl": GB})
    text = reader_tools.get_translations(project, culture="EN-gb")
    assert text.startswith("=== TRANSLATIONS en-GB: captions for 4 of 20 visible objects")
    assert "Model: 'Data model'" in text
    assert "Sales -> 'Turnover' | description: The turnover" in text
    assert "  measure Total Sales -> 'Total turnover' | folder: Base" in text
    assert "  column Old Column -> 'Former'  [!] not in the model" in text
    assert "  no caption: column OrderDate, column Net Amount, measure Sales LY" in text
    assert "\nDate\n  no caption: the table itself, column Date, column Year" in text
    assert "[!] translations of objects not in the model: Sales[Old Column] (column), Gone (table)" in text
    assert "LocalDateTable_0a1b" not in text                               # hidden: nobody sees it
    sales = reader_tools.get_translations(project, culture="en-GB", table_name="sales")
    assert "Customer" not in sales and "Gone (table)" not in sales and "Sales[Old Column]" in sales
    with pytest.raises(ElementNotFoundError, match="cultures: en-GB, en-US"):
        reader_tools.get_translations(project, culture="fr-FR")


def test_no_cultures(make_project):
    project = make_project({f"{MODEL}/cultures/en-US.tmdl": None})
    assert reader_tools.get_translations(project) == (
        "No cultures in the model ('definition/cultures' is missing or empty): "
        "no translations; the model culture is en-US.")
