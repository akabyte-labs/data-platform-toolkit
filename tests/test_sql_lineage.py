"""
Lineage of a database project: the T-SQL that the analysis must follow
(views, procedures with control flow and temporary tables, MERGE, files read
with OPENROWSET, external tables), the patterns of lineage.yaml, the checks
of the configuration and the MCP tools.
"""

from __future__ import annotations

import json
import os

import pytest

from mcp_data_platform_toolkit.lineage import graph as lineage_graph
from mcp_data_platform_toolkit.lineage import lineage_tools
from mcp_data_platform_toolkit.sql_project import config as lineage_config
from mcp_data_platform_toolkit.sql_project import tsql_scanner as scanner
from mcp_data_platform_toolkit.sql_project.project import load_sql_lineage
from mcp_data_platform_toolkit.sql_project.sql_files import find_database_project, read_sql_text

SQLPROJ = """<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup>
    <Build Include="raw\\Tables\\customers.sql" />
    <PostDeploy Include="Scripts\\Script.PostDeployment.sql" />
  </ItemGroup>
</Project>
"""

PROJECT = {
    "Shop.sqlproj": SQLPROJ,
    "raw/Tables/customers.sql":
        "CREATE TABLE [raw].[customers] ([CustId] INT NOT NULL, [Name] NVARCHAR(100),\n"
        "    [Country] NVARCHAR(50), [etl_loaded_at] DATETIME2)\n",
    "raw/Tables/orders.sql":
        "CREATE EXTERNAL TABLE raw.orders (OrderId INT, CustId INT, Qty INT, Price MONEY)\n"
        "WITH (LOCATION = 'sales/orders/', DATA_SOURCE = lake, FILE_FORMAT = parquet)\n",
    "core/Views/customer.sql":
        "CREATE VIEW core.customer AS\n"
        "SELECT c.CustId AS CustomerKey, UPPER(c.Name) AS CustomerName, c.Country\n"
        "FROM raw.customers c\n",
    "core/Tables/sales.sql":
        "CREATE TABLE core.sales (OrderId INT, CustomerKey INT, Amount MONEY, LoadedAt DATETIME2)\n",
    "core/Tables/sales_history.sql":
        "CREATE TABLE core.sales_history (OrderId INT, CustomerKey INT, Amount MONEY, ValidFrom DATETIME2)\n",
    "core/Stored Procedures/load_sales.sql": """CREATE PROCEDURE core.load_sales @since AS date = NULL
WITH EXECUTE AS OWNER
AS
BEGIN
  SET NOCOUNT ON;
  IF @since IS NULL SET @since = '19000101';
  BEGIN TRY
    SELECT o.OrderId, o.CustId, o.Qty * o.Price AS Amount
    INTO #s
    FROM raw.orders o WITH (NOLOCK)
    WHERE o.Qty > 0
    MERGE core.sales AS t
    USING #s AS s ON t.OrderId = s.OrderId
    WHEN MATCHED THEN UPDATE SET t.Amount = s.Amount, t.LoadedAt = GETDATE()
    WHEN NOT MATCHED THEN INSERT (OrderId, CustomerKey, Amount) VALUES (s.OrderId, s.CustId, s.Amount);
  END TRY
  BEGIN CATCH
    THROW;
  END CATCH
  DECLARE @sql nvarchar(max) = N'TRUNCATE TABLE core.sales';
  EXEC sp_executesql @sql;
END
GO
""",
    "mart/Views/sales.sql": """CREATE VIEW mart.sales AS
WITH s AS (SELECT * FROM core.sales)
SELECT s.OrderId, s.Amount, c.CustomerName,
       CASE WHEN c.Country = 'IT' THEN 'Domestic' ELSE 'Export' END AS Market
FROM s JOIN core.customer c ON c.CustomerKey = s.CustomerKey
""",
    "mart/Views/targets.sql": """CREATE VIEW mart.targets AS
SELECT r.[Year], r.Target
FROM OPENROWSET(BULK 'targets/*.csv', DATA_SOURCE = 'lake', FORMAT = 'CSV')
     WITH ([Year] INT, Target DECIMAL(18, 2)) AS r
""",
    "Scripts/Script.PostDeployment.sql": ":r .\\seed.sql\n",
}

CONFIG = """layers:
  bronze: raw
  silver: core
  gold: mart
technical_columns: ['etl_\\w+', 'LoadedAt']
rules:
  - name: history
    match: 'core\\.(?P<entity>\\w+)_history'
    from: 'core.{entity}'
    columns:
      ValidFrom: LoadedAt
"""


@pytest.fixture
def shop(write_files, tmp_path):
    return write_files({**PROJECT, "lineage.yaml": CONFIG}, str(tmp_path / "shop"))


def _edges(graph) -> set[tuple[str, str, str]]:
    return {(graph.nodes[e.source].label, graph.nodes[e.target].label, e.rule or e.kind)
            for e in graph.edges}


def _sources_of(lineage, column: str) -> set[str]:
    columns = lineage.columns
    target = next(k for k, n in columns.nodes.items() if n.label == column)
    return {columns.nodes[e.source].label for e in columns.edges if e.target == target}


# ---------------------------------------------------------------------------
# What the SQL says
# ---------------------------------------------------------------------------

def test_object_lineage_through_views_procedures_and_files(shop):
    lineage = load_sql_lineage(shop)
    assert _edges(lineage.objects) == {
        ("file:sales/orders/", "raw.orders", "location"),
        ("file:targets/*.csv", "mart.targets", "view"),
        ("raw.customers", "core.customer", "view"),
        ("raw.orders", "core.sales", "merge"),
        ("core.customer", "mart.sales", "view"),
        ("core.sales", "mart.sales", "view"),
        ("core.sales", "core.sales_history", "history"),
    }
    # The temporary table is a step of the procedure, not an object.
    assert not any(n.label.startswith("#") for n in lineage.objects.nodes.values())


def test_columns_follow_merge_through_a_temporary_table(shop):
    lineage = load_sql_lineage(shop)
    assert _sources_of(lineage, "core.sales.Amount") == {"raw.orders.Qty", "raw.orders.Price"}
    assert _sources_of(lineage, "core.sales.CustomerKey") == {"raw.orders.CustId"}
    merge = next(e for e in lineage.columns.edges
                 if lineage.columns.nodes[e.target].label == "core.sales.Amount")
    assert merge.evidence == "core/Stored Procedures/load_sales.sql:12"


def test_columns_through_cte_star_case_and_expressions(shop):
    lineage = load_sql_lineage(shop)
    assert _sources_of(lineage, "mart.sales.Amount") == {"core.sales.Amount"}
    assert _sources_of(lineage, "mart.sales.Market") == {"core.customer.Country"}
    assert _sources_of(lineage, "core.customer.CustomerName") == {"raw.customers.Name"}


def test_files_read_with_openrowset_and_external_tables(shop):
    lineage = load_sql_lineage(shop)
    assert _sources_of(lineage, "mart.targets.Year") == {"file:targets/*.csv (Year)"}
    assert _sources_of(lineage, "raw.orders.Qty") == {"file:sales/orders/ (Qty)"}
    assert lineage.objects.nodes["file:targets/*.csv"].layer == "source"


def test_what_cannot_be_analyzed_is_reported_with_its_line(shop):
    problems = load_sql_lineage(shop).analysis.problems
    assert problems["dynamic_sql"] == ["core/Stored Procedures/load_sales.sql:21: core.load_sales"]
    assert problems["excluded_files"] == ["Scripts/Script.PostDeployment.sql"]
    assert "parse_errors" not in problems


def test_procedure_statements_without_semicolons(write_files, tmp_path):
    """The next statement, an UPDATE's own SET, UNION, CASE ... END and table hints."""
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "t.sql": "CREATE TABLE dbo.a (x INT, y INT)\nGO\nCREATE TABLE dbo.b (x INT, y INT)\n",
        "p.sql": """CREATE PROCEDURE dbo.p AS
BEGIN
  INSERT INTO dbo.b (x, y)
  SELECT x, CASE WHEN y > 0 THEN y END FROM dbo.a WITH (NOLOCK)
  UNION ALL SELECT x, y FROM dbo.c
  UPDATE b SET b.y = a.y FROM dbo.b AS b JOIN dbo.a AS a ON a.x = b.x
  SET @n = @@ROWCOUNT
  INSERT INTO dbo.b EXEC dbo.q
END
""",
    }, str(tmp_path / "p"))
    lineage = load_sql_lineage(root)
    assert ("dbo.a", "dbo.b", "insert") in _edges(lineage.objects)
    assert ("dbo.c", "dbo.b", "insert") in _edges(lineage.objects)
    assert ("dbo.a", "dbo.b", "update") in _edges(lineage.objects)
    assert lineage.objects.nodes["dbo.c"].flags == ["not in project"]
    assert lineage.analysis.problems["not_analyzed"] == ["p.sql:8: INSERT ... EXEC in dbo.p"]
    assert "parse_errors" not in lineage.analysis.problems


def test_apply_output_clauses_table_functions_and_other_databases(write_files, tmp_path):
    """
    An APPLY subquery attributed its column to the outer column of its WHERE;
    OUTPUT ... INTO made MERGE and INSERT unreadable; a four-part name lost a part.
    """
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "t.sql": "CREATE TABLE dbo.a (id INT, x INT)\nGO\nCREATE TABLE dbo.b (id INT, y INT)\nGO\n"
                 "CREATE TABLE dbo.log (id INT)\nGO\n"
                 "CREATE FUNCTION dbo.f(@id INT) RETURNS TABLE AS RETURN SELECT b.y AS fy FROM dbo.b b WHERE b.id = @id\n",
        "v.sql": "CREATE VIEW dbo.v AS SELECT a.id, o.y, z.fy FROM dbo.a a "
                 "CROSS APPLY (SELECT TOP 1 b.y FROM dbo.b b WHERE b.id = a.id) o OUTER APPLY dbo.f(a.id) AS z\n",
        "remote.sql": "CREATE VIEW dbo.r AS SELECT l.c FROM srv.db.dbo.linked l JOIN otherdb.dbo.t t ON t.id = l.id\n",
        "p.sql": "CREATE PROCEDURE dbo.p AS BEGIN\n"
                 "  MERGE dbo.b AS t USING dbo.a AS s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.y = s.x\n"
                 "    OUTPUT inserted.id INTO dbo.log (id);\n"
                 "  INSERT INTO dbo.b (id, y) OUTPUT inserted.id INTO dbo.log (id) SELECT id, x FROM dbo.a\n"
                 "END\n",
    }, str(tmp_path / "apply"))
    lineage = load_sql_lineage(root)
    assert _sources_of(lineage, "dbo.v.y") == {"dbo.b.y"}
    assert _sources_of(lineage, "dbo.v.fy") == {"dbo.f.fy"}
    assert ("dbo.f", "dbo.v", "view") in _edges(lineage.objects)
    assert {("dbo.a", "dbo.b", "merge"), ("dbo.a", "dbo.b", "insert")} <= _edges(lineage.objects)
    assert _sources_of(lineage, "dbo.b.y") == {"dbo.a.x"}
    assert {s for s, t, _k in _edges(lineage.objects) if t == "dbo.r"} == {"srv.db.dbo.linked", "otherdb.dbo.t"}
    assert "parse_errors" not in lineage.analysis.problems


def test_names_without_schema(write_files, tmp_path):
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE dbo.plain (a INT)\nGO\nCREATE TABLE raw.only_here (a INT)\nGO\n"
                 "CREATE TABLE raw.twice (a INT)\nGO\nCREATE TABLE core.twice (a INT)\n",
        "v.sql": "CREATE VIEW core.v AS SELECT p.a, o.a AS b, t.a AS c FROM plain p "
                 "JOIN only_here o ON o.a = p.a JOIN twice t ON t.a = p.a\n",
    }, str(tmp_path / "n"))
    lineage = load_sql_lineage(root)
    sources = {s for s, t, _k in _edges(lineage.objects) if t == "core.v"}
    assert {"dbo.plain", "raw.only_here", "dbo.twice"} == sources
    assert lineage.objects.nodes["dbo.twice"].flags == ["ambiguous"]
    assert "raw.twice, core.twice" in lineage.analysis.problems["ambiguous"][0] or \
        "core.twice, raw.twice" in lineage.analysis.problems["ambiguous"][0]


def test_a_name_without_schema_is_not_taken_by_an_object_outside_the_project(write_files, tmp_path):
    """`dbo.x`, referenced but not defined, used to capture every later `x` without schema."""
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE raw.x (a INT)\n",
        "b.sql": "CREATE VIEW core.first AS SELECT a FROM dbo.x\n",
        "c.sql": "CREATE VIEW core.second AS SELECT a FROM x\n",
    }, str(tmp_path / "o"))
    edges = _edges(load_sql_lineage(root).objects)
    assert ("dbo.x", "core.first", "view") in edges
    assert ("raw.x", "core.second", "view") in edges


def test_a_column_called_output_is_not_an_output_clause(write_files, tmp_path):
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE dbo.plant (Id INT, Output INT)\n",
        "v.sql": "CREATE VIEW dbo.v AS SELECT Id, Output FROM dbo.plant WHERE Output > 0\n",
    }, str(tmp_path / "out"))
    lineage = load_sql_lineage(root)
    assert _sources_of(lineage, "dbo.v.Output") == {"dbo.plant.Output"}
    assert "parse_errors" not in lineage.analysis.problems


def test_broken_duplicate_and_oversized_files_are_reported(write_files, tmp_path, monkeypatch):
    from mcp_data_platform_toolkit.sql_project import analyzer
    monkeypatch.setattr(analyzer, "MAX_SQL_FILE_BYTES", 200)
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE dbo.a (x INT)\n",
        "a_again.sql": "CREATE TABLE dbo.a (y INT)\n",
        "broken.sql": "CREATE VIEW dbo.v AS SELECT FROM WHERE (\n",
        "big.sql": "CREATE VIEW dbo.big AS SELECT 1 AS x\n" + "-- padding\n" * 40,
        "ok.sql": "CREATE VIEW dbo.ok AS SELECT x FROM dbo.a\n",
    }, str(tmp_path / "b"))
    lineage = load_sql_lineage(root)
    problems = lineage.analysis.problems
    assert problems["duplicates"] == ["dbo.a (a.sql:1 / a_again.sql:1)"]
    assert problems["parse_errors"][0].startswith("broken.sql:1:")
    assert problems["skipped_files"][0].startswith("big.sql:")
    assert ("dbo.a", "dbo.ok", "view") in _edges(lineage.objects)
    assert lineage.analysis.objects["dbo.a"].columns == ["x"]


def test_a_statement_after_go_has_its_own_line(write_files, tmp_path):
    """It used to get the line of the GO before it."""
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE dbo.a (x INT)\nGO\n\n-- the view\nCREATE VIEW dbo.v AS SELECT x FROM dbo.a\n",
    }, str(tmp_path / "g"))
    edge = load_sql_lineage(root).objects.edges[0]
    assert edge.evidence == "a.sql:5"


def test_an_unexpected_failure_stays_in_its_statement(shop, monkeypatch):
    """One statement the analysis can't handle must not take the project's lineage with it."""
    from mcp_data_platform_toolkit.sql_project import analyzer

    def broken(_merge):
        raise RuntimeError("unexpected shape")
    monkeypatch.setattr(analyzer, "_merge_query", broken)
    lineage = load_sql_lineage(shop)
    assert lineage.analysis.problems["column_errors"] == [
        "core/Stored Procedures/load_sales.sql:12: RuntimeError: unexpected shape"]
    assert ("raw.customers", "core.customer", "view") in _edges(lineage.objects)


def test_no_self_edge_for_tables_and_incremental_loads(write_files, tmp_path):
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE dbo.a (x INT, y AS (x * 2))\n",
        "load.sql": "INSERT INTO dbo.a (x) SELECT s.x FROM dbo.src s "
                    "WHERE NOT EXISTS (SELECT 1 FROM dbo.a t WHERE t.x = s.x)\n",
    }, str(tmp_path / "s"))
    lineage = load_sql_lineage(root)
    assert all(e.source != e.target for e in lineage.objects.edges)
    assert _edges(lineage.objects) == {("dbo.src", "dbo.a", "insert")}
    assert _sources_of(lineage, "dbo.a.y") == {"dbo.a.x"}           # computed column


def test_utf16_and_cp1252_files(tmp_path):
    utf16 = tmp_path / "u.sql"
    utf16.write_bytes("CREATE VIEW dbo.é AS SELECT 1 AS x".encode("utf-16"))
    cp1252 = tmp_path / "c.sql"
    cp1252.write_bytes("-- café\nSELECT 1".encode("cp1252"))
    assert read_sql_text(str(utf16)).startswith("CREATE VIEW dbo.é")
    assert "café" in read_sql_text(str(cp1252))


# ---------------------------------------------------------------------------
# Patterns of lineage.yaml
# ---------------------------------------------------------------------------

def test_a_rule_links_objects_and_renamed_columns(shop):
    lineage = load_sql_lineage(shop)
    assert _sources_of(lineage, "core.sales_history.ValidFrom") == {"core.sales.LoadedAt"}
    assert _sources_of(lineage, "core.sales_history.Amount") == {"core.sales.Amount"}
    edge = next(e for e in lineage.objects.edges if e.rule)
    assert (edge.confidence, edge.evidence) == ("rule", "rule 'history'")


def test_a_rule_must_match_the_whole_name_and_never_creates_its_source(write_files, tmp_path):
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "t.sql": "CREATE TABLE core.orders_history (a INT)\nGO\n"
                 "CREATE TABLE core.history_totals (a INT)\nGO\n"
                 "CREATE TABLE core.payments_history (a INT)\nGO\nCREATE TABLE core.orders (a INT)\n",
        "lineage.yaml": "layers:\n  silver: core\nrules:\n  - name: h\n"
                        "    match: 'core\\.(?P<e>\\w+)_history'\n    from: 'core.{e}'\n",
    }, str(tmp_path / "r"))
    lineage = load_sql_lineage(root)
    report = lineage.analysis.rules["h"]
    assert sorted(report["matched"]) == ["core.orders_history", "core.payments_history"]
    assert report["missing"] == ["core.payments_history -> core.payments"]
    assert "core.payments" not in lineage.objects.nodes


def test_an_external_rule_starts_the_lineage_at_the_source_system(write_files, tmp_path):
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "t.sql": "CREATE TABLE raw.erp_customers (Id INT, Name NVARCHAR(50))\n",
        "lineage.yaml": "layers:\n  bronze: raw\nrules:\n  - name: systems\n"
                        "    match: 'raw\\.(?P<system>[a-z]+)_(?P<table>\\w+)'\n"
                        "    from: '{system}.{table}'\n    external: true\n",
    }, str(tmp_path / "e"))
    lineage = load_sql_lineage(root)
    assert _edges(lineage.objects) == {("erp.customers", "raw.erp_customers", "systems")}
    assert lineage.objects.nodes["source:erp.customers"].layer == "source"
    assert _sources_of(lineage, "raw.erp_customers.Name") == {"erp.customers.Name"}


def test_a_rule_gives_its_columns_to_select_star_over_files(write_files, tmp_path):
    """Files written by a pipeline and read back with SELECT *: the columns come from the rule."""
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE VIEW clean.v_orders AS SELECT OrderId, Amount FROM dbo.orders_src\nGO\n"
                 "CREATE TABLE dbo.orders_src (OrderId INT, Amount MONEY)\n",
        "b.sql": "CREATE VIEW curated.orders AS SELECT * FROM OPENROWSET(BULK 'curated/orders/*.parquet', "
                 "FORMAT = 'PARQUET') AS r\n",
        "c.sql": "CREATE VIEW mart.orders AS SELECT * FROM curated.orders\n",
        "lineage.yaml": "layers:\n  silver: [clean, curated]\n  gold: mart\nignore: dbo\nrules:\n"
                        "  - name: exported\n    match: 'curated\\.(?P<e>\\w+)'\n    from: 'clean.v_{e}'\n",
    }, str(tmp_path / "x"))
    lineage = load_sql_lineage(root)
    assert _sources_of(lineage, "curated.orders.Amount") == {"clean.v_orders.Amount"}
    assert _sources_of(lineage, "mart.orders.Amount") == {"curated.orders.Amount"}


def test_without_a_file_the_standard_medallion_applies(write_files, tmp_path):
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE bronze.a (x INT)\nGO\nCREATE VIEW silver.b AS SELECT x FROM bronze.a\n"
                 "GO\nCREATE VIEW gold.c AS SELECT x FROM silver.b\nGO\nCREATE TABLE dbo.other (x INT)\n",
    }, str(tmp_path / "d"))
    lineage = load_sql_lineage(root)
    assert lineage.config.path == ""
    assert [lineage.objects.nodes[k].layer for k in ("bronze.a", "silver.b", "gold.c")] == \
        ["bronze", "silver", "gold"]
    assert lineage.checks["unassigned"] == {"dbo": ["dbo.other"]}


# ---------------------------------------------------------------------------
# lineage.yaml: every problem, with its line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("layers:\n  bronze: [raw\n", "line 3: not valid YAML"),
    ("layer:\n  bronze: raw\n", "line 1, layer: unknown key"),
    ("layers:\n  bronze: raw\n  silver: raw\n", "line 3, layers.silver: schema 'raw' is already in layer 'bronze'"),
    ("layers:\n  source: raw\n", "'source' is reserved"),
    ("layers:\n  bronze: raw\nignore: raw\n", "schema 'raw' is also in layer 'bronze'"),
    ("rules:\n  - name: a\n    match: '(unclosed'\n    from: 'x.y'\n", "line 3, rules[0].match: invalid regular expression"),
    ("rules:\n  - name: a\n    match: 'x\\.(?P<e>\\w+)'\n    from: 'y.{entity}'\n", "{entity} is not a named group"),
    ("rules:\n  - name: a\n    match: 'x'\n    from: 'nodot'\n", "must give 'schema.object'"),
    ("rules:\n  - name: a\n    match: 'x'\n", "line 2, rules[0]: missing from"),
    ("rules:\n  - name: a\n    match: 'x'\n    from: 'a.b'\n    form: 'c'\n", "line 5, rules[0].form: unknown key"),
    ("layers:\n  bronze: raw\nlayers:\n  silver: core\n", "line 3, layers: written twice"),
    ("version: 2\n", "only version 1 exists"),
])
def test_configuration_problems(text, expected):
    with pytest.raises(lineage_config.ConfigError) as error:
        lineage_config.parse_config(text, "lineage.yaml")
    assert any(expected in problem for problem in error.value.problems), error.value.problems


def test_every_problem_is_reported_at_once():
    with pytest.raises(lineage_config.ConfigError) as error:
        lineage_config.parse_config("layer: x\nrules:\n  - name: a\n    match: '('\n    from: 'a.b'\n")
    assert len(error.value.problems) == 2


def test_the_example_configuration_is_valid():
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "lineage.example.yaml")
    config = lineage_config.load_config(path)
    assert config.layer_names == ["bronze", "silver", "gold"]
    assert [r.name for r in config.rules] == ["history-tables", "source-systems"]


# ---------------------------------------------------------------------------
# Checks of the configuration against the project
# ---------------------------------------------------------------------------

def test_checks_point_at_what_the_configuration_misses(write_files, tmp_path):
    root = write_files({
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE raw.a (x INT, etl_at DATETIME)\nGO\n"
                 "CREATE TABLE core.loaded_by_pipeline (x INT)\nGO\n"
                 "CREATE VIEW raw.reads_gold AS SELECT x FROM mart.m\nGO\n"
                 "CREATE VIEW mart.m AS SELECT x, etl_at FROM raw.a\nGO\n"
                 "CREATE TABLE misc.thing (x INT)\nGO\nCREATE TABLE config.settings (x INT)\n",
        "lineage.yaml": "layers:\n  bronze: raw\n  silver: core\n  gold: mart\n  extra: nothing_here\n"
                        "ignore: config\ntechnical_columns: ['etl_\\w+']\n",
    }, str(tmp_path / "c"))
    checks = load_sql_lineage(root).checks
    assert checks["unassigned"] == {"misc": ["misc.thing"]}
    assert checks["ignored"] == {"config": 1}
    assert checks["loads_not_found"] == ["core.loaded_by_pipeline"]
    assert len(checks["backward"]) == 1 and checks["backward"][0].startswith("mart.m [gold] -> raw.reads_gold [bronze]")
    assert checks["by_layer"]["extra"] == []
    assert checks["columns_without_source"] == {"silver": ["core.loaded_by_pipeline.x"]}

    report = lineage_tools.test_lineage_config(root)
    assert "[!] Schemas in no layer" in report and "misc: misc.thing" in report
    assert "extra [nothing_here]: 0 objects  [!] no object" in report
    assert "[!] Tables nothing loads in the SQL" in report
    assert "[!] Flows going backwards" in report


def test_the_check_reports_a_broken_file_instead_of_failing(write_files, tmp_path):
    root = write_files({"P.sqlproj": SQLPROJ, "a.sql": "CREATE TABLE dbo.a (x INT)\n",
                        "lineage.yaml": "layers:\n  bronze: [raw\n"}, str(tmp_path / "k"))
    report = lineage_tools.test_lineage_config(root)
    assert "[!] The file has 1 problem(s)" in report and "line 3: not valid YAML" in report
    with pytest.raises(lineage_config.ConfigError):
        lineage_tools.get_sql_project_lineage(root)


def test_a_rule_that_matches_nothing_is_flagged(shop):
    with open(os.path.join(shop, "lineage.yaml"), "a", encoding="utf-8") as handle:
        handle.write("  - name: typo\n    match: 'core.sales_hist'\n    from: 'core.sales'\n")
    report = lineage_tools.test_lineage_config(shop)
    assert "[!] typo: matches no object" in report
    assert "history: 1 objects, 1 with their source: core.sales_history" in report


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def test_tool_whole_project_one_line_per_edge(shop):
    text = lineage_tools.get_sql_project_lineage(shop)
    assert text.startswith("=== SQL LINEAGE: Shop (object level) ===")
    assert "raw.orders [bronze] -> core.sales [silver]  merge  core/Stored Procedures/load_sales.sql:12" in text
    assert "core.sales [silver] -> core.sales_history [silver]  rule 'history'" in text
    assert "Warnings: 1 dynamic sql" in text


def test_tool_column_upstream_tree(shop):
    text = lineage_tools.get_sql_project_lineage(
        shop, target="[mart].[sales].[Amount]", direction="upstream", output_format="tree")
    assert "mart.sales.Amount [gold]\n   <- core.sales.Amount [silver]  view" in text
    assert "         <- file:sales/orders/ (Qty) [source]  location" in text


def test_tool_impact_downstream_of_a_source_column(shop):
    text = lineage_tools.get_sql_project_lineage(shop, target="raw.orders.Qty", direction="downstream")
    assert "core.sales.Amount [silver] -> mart.sales.Amount [gold]" in text
    assert "core.sales.Amount [silver] -> core.sales_history.Amount [silver]  rule 'history'" in text
    assert "customer" not in text


def test_tool_columns_of_an_object_and_json(shop):
    payload = json.loads(lineage_tools.get_sql_project_lineage(
        shop, target="core.customer", level="column", direction="upstream", output_format="json"))
    assert {e["target"] for e in payload["edges"]} == {
        "core.customer|customerkey", "core.customer|customername", "core.customer|country"}
    assert payload["warnings"]["dynamic_sql"]


def test_tool_target_errors(shop):
    with pytest.raises(LookupError, match="not an object"):
        lineage_tools.get_sql_project_lineage(shop, target="core.nothing")
    with pytest.raises(LookupError, match="its columns are OrderId, CustomerKey, Amount, LoadedAt"):
        lineage_tools.get_sql_project_lineage(shop, target="core.sales.Nope")
    with pytest.raises(ValueError, match="matches several objects"):
        lineage_tools.get_sql_project_lineage(shop, target="sales")
    with pytest.raises(ValueError, match="'direction' must be one of"):
        lineage_tools.get_sql_project_lineage(shop, direction="sideways")


def test_tool_reduces_detail_and_keeps_every_name(shop, monkeypatch):
    full = lineage_tools.get_sql_project_lineage(shop, level="column")
    monkeypatch.setattr(lineage_tools, "MAX_TOOL_OUTPUT_CHARS", len(full) * 2 // 3)
    reduced = lineage_tools.get_sql_project_lineage(shop, level="column")
    assert len(reduced) <= len(full) * 2 // 3
    assert "[response reduced" in reduced and "No node was omitted" in reduced
    for label in ("mart.sales.Market", "raw.orders.Qty", "core.sales_history.ValidFrom"):
        assert label in reduced


def test_tools_registered_through_mcp(shop):
    import asyncio
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer("test")
    lineage_tools.register_lineage_tools(mcp)
    assert {t.name for t in asyncio.run(mcp.list_tools())} == {
        "get_sql_project_lineage", "get_end_to_end_lineage", "test_lineage_config",
        "suggest_lineage_rules", "draft_lineage_config", "export_lineage", "get_lineage_config_guide"}
    result = asyncio.run(mcp.call_tool("get_sql_project_lineage",
                                       {"sql_project_path": shop, "target": "mart.targets"}))
    blocks = getattr(result, "content", None) or (result[0] if isinstance(result, tuple) else result)
    assert "file:targets/*.csv [source] -> mart.targets [gold]" in " ".join(b.text for b in blocks)


def test_the_guide_is_served():
    text = lineage_tools.get_lineage_config_guide()
    assert text.startswith("# How to write lineage.yaml") and "test_lineage_config" in text


# ---------------------------------------------------------------------------
# Finding the project
# ---------------------------------------------------------------------------

def test_the_project_is_identified_by_its_sqlproj(write_files, tmp_path, monkeypatch):
    base = str(tmp_path / "repo")
    write_files({"db/One.sqlproj": SQLPROJ, "db/a.sql": "SELECT 1\n"}, base)
    assert find_database_project(base).name == "One"
    write_files({"other/Two.sqlproj": SQLPROJ}, base)
    with pytest.raises(ValueError, match="there are 2 database projects"):
        find_database_project(base)
    scripts = write_files({"x.sql": "SELECT 1\n"}, str(tmp_path / "scripts"))
    with pytest.raises(FileNotFoundError, match="No database project"):
        find_database_project(scripts)
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path / "elsewhere"))
    with pytest.raises(ValueError, match="outside the folders"):
        find_database_project(os.path.join(base, "db"))


# ---------------------------------------------------------------------------
# Scanner and graph
# ---------------------------------------------------------------------------

def test_batches_keep_their_line_numbers():
    text = ":setvar X 1\nCREATE TABLE a (x INT)\nGO\n\n-- c\nCREATE VIEW v AS SELECT 1 AS x\nGO 2\nSELECT 1"
    batches = scanner.split_batches(text)
    assert [(scanner.first_line(b), b.text.strip().splitlines()[-1]) for b in batches] == [
        (2, "CREATE TABLE a (x INT)"), (6, "CREATE VIEW v AS SELECT 1 AS x"), (8, "SELECT 1")]


def test_openrowset_replacement_keeps_lines_and_columns():
    sql = "SELECT * FROM OPENROWSET(BULK N'a/b''c.csv',\n  FORMAT = 'CSV')\n  WITH (A int, [B] decimal(10, 2)) AS r\nWHERE 1 = 1"
    rewritten, reads = scanner.rewrite_openrowset(sql, [0])
    assert rewritten.count("\n") == sql.count("\n")
    assert (reads[0].path, reads[0].columns) == ("a/b'c.csv", ["A", "B"])
    assert f"[{scanner.FILE_SCHEMA}].[f0]" in rewritten


def test_a_tree_expands_every_node_once():
    """A chain of diamonds: reprinting each subtree made the output grow exponentially."""
    nodes, edges = {}, []
    for level in range(12):
        for side in ("a", "b"):
            nodes[f"{side}{level}"] = lineage_graph.Node(f"{side}{level}", f"{side}{level}")
        if level:
            for source in ("a", "b"):
                for target in ("a", "b"):
                    edges.append(lineage_graph.GraphEdge(f"{source}{level - 1}", f"{target}{level}", "view"))
    graph = lineage_graph.Graph(nodes, edges)
    text = lineage_graph.format_tree(graph, ["a11"], "upstream", ["T"])
    assert len(text.splitlines()) < 60
    assert "(expanded above)" in text


# ---------------------------------------------------------------------------
# Patterns of real projects (WideWorldImporters, WideWorldImportersDW)
# ---------------------------------------------------------------------------

def test_a_table_the_parser_cannot_read_is_still_defined(write_files, tmp_path):
    """A storage clause inside a constraint, a masked column: the table must not vanish."""
    root = write_files({
        "P.sqlproj": PROJECT["Shop.sqlproj"],
        "f.sql": "﻿CREATE TABLE [Fact].[Movement] (\n"
                 "    [Movement Key] BIGINT IDENTITY (1, 1) NOT NULL,\n"
                 "    [Date Key] DATE NOT NULL,\n"
                 "    [Quantity] INT NOT NULL,\n"
                 "    CONSTRAINT [PK_Fact_Movement] PRIMARY KEY NONCLUSTERED ([Movement Key] ASC, [Date Key] ASC)"
                 " ON [PS_Date] ([Date Key])\n"
                 ") ON [PS_Date] ([Date Key]);\nGO\n",
        "s.sql": "CREATE TABLE [Purchasing].[Suppliers] (\n"
                 "    [SupplierID] INT NOT NULL,\n"
                 "    [BankAccountName] NVARCHAR (50) MASKED WITH (FUNCTION = 'default()') NULL,\n"
                 "    [ValidFrom] DATETIME2 (7) GENERATED ALWAYS AS ROW START NOT NULL,\n"
                 "    [ValidTo] DATETIME2 (7) GENERATED ALWAYS AS ROW END NOT NULL,\n"
                 "    CONSTRAINT [PK_Purchasing_Suppliers] PRIMARY KEY CLUSTERED ([SupplierID] ASC),\n"
                 "    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])\n"
                 ") WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE=[Purchasing].[Suppliers_Archive]));\nGO\n",
        "v.sql": "CREATE VIEW dbo.v AS SELECT [Quantity], [SupplierID] FROM [Fact].[Movement], [Purchasing].[Suppliers]\n",
    }, str(tmp_path / "p"))
    lineage = load_sql_lineage(root)
    objects = lineage.analysis.objects
    assert objects["fact.movement"].in_project and objects["fact.movement"].columns == \
        ["Movement Key", "Date Key", "Quantity"]
    assert objects["purchasing.suppliers"].in_project and objects["purchasing.suppliers"].columns == \
        ["SupplierID", "BankAccountName", "ValidFrom", "ValidTo"]
    assert "not in project" not in lineage.objects.nodes["fact.movement"].flags
    read = lineage.analysis.problems["read_without_parser"]
    assert any("Fact.Movement (3 columns" in p for p in read) and len(read) == 2
    assert "parse_errors" not in lineage.analysis.problems
    assert ("fact.movement|quantity", "dbo.v|quantity") in {(e.source, e.target) for e in lineage.columns.edges}


def test_the_ctes_of_an_update_a_merge_and_an_insert_are_not_tables(write_files, tmp_path):
    """WITH ... UPDATE, as in the slowly changing dimensions of WideWorldImportersDW."""
    root = write_files({
        "P.sqlproj": PROJECT["Shop.sqlproj"],
        "t.sql": "CREATE TABLE Integration.City_Staging ([WWI City ID] INT, [Valid From] DATETIME2)\nGO\n"
                 "CREATE TABLE Dimension.City ([WWI City ID] INT, [Valid To] DATETIME2)\nGO\n"
                 "CREATE TABLE s.src (k INT, q INT)\nGO\nCREATE TABLE d.f (k INT, q INT)\nGO\n"
                 "CREATE TABLE d.u (k INT)\nGO\n",
        "p.sql": """CREATE PROCEDURE Integration.MigrateStagedCityData AS
BEGIN
    WITH RowsToCloseOff AS (
        SELECT c.[WWI City ID], MIN(c.[Valid From]) AS [Valid From]
        FROM Integration.City_Staging AS c GROUP BY c.[WWI City ID])
    UPDATE c SET c.[Valid To] = rtco.[Valid From]
    FROM Dimension.City AS c INNER JOIN RowsToCloseOff AS rtco ON c.[WWI City ID] = rtco.[WWI City ID];

    WITH M AS (SELECT k, q FROM s.src)
    MERGE d.f AS t USING M AS m ON t.k = m.k
    WHEN MATCHED THEN UPDATE SET q = m.q WHEN NOT MATCHED THEN INSERT (k, q) VALUES (m.k, m.q);

    WITH a AS (SELECT k FROM s.src), b AS (SELECT k FROM a)
    INSERT INTO d.u (k) SELECT k FROM b;
END
""",
    }, str(tmp_path / "p"))
    lineage = load_sql_lineage(root)
    unknown = [o.label for o in lineage.analysis.objects.values() if not o.in_project]
    assert unknown == [], "a CTE is not an object outside the project"
    columns = {(e.source, e.target) for e in lineage.columns.edges}
    assert ("integration.city_staging|valid from", "dimension.city|valid to") in columns
    assert {("s.src|q", "d.f|q"), ("s.src|k", "d.u|k")} <= columns
