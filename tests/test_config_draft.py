"""
The draft of lineage.yaml: layers proposed from the schema names and the
flows of the SQL, questions on what stays uncertain, the answers that
complete it, and the file saved and checked against the project.
"""

from __future__ import annotations

import json
import os

import pytest

from mcp_data_platform_toolkit.lineage import config_draft, lineage_tools
from mcp_data_platform_toolkit.lineage.config_draft import SchemaInfo, draft_config
from mcp_data_platform_toolkit.mcp_common import user_folder
from mcp_data_platform_toolkit.sql_project import config as lineage_config
from mcp_data_platform_toolkit.sql_project.project import DATA_KINDS, load_sql_lineage
from tests.test_end_to_end import FILES as E2E_FILES
from tests.test_sql_lineage import PROJECT as SHOP

SQLPROJ = """<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup><DefaultSchema>dbo</DefaultSchema></PropertyGroup>
</Project>
"""

# A warehouse with its own conventions: bronze tables named after the source
# systems, views that prepare the loads, numbered staging tables that a generic
# procedure fills from those views (no SQL says so), dimensions and facts with
# ETL columns, views for Power BI, configuration and logging.
DWH = {
    "Dwh.sqlproj": SQLPROJ,
    "stg/erp_customers.sql":
        "CREATE TABLE stg.erp_customers (CustomerId INT, Name NVARCHAR(50), etl_load_date DATETIME2)\n",
    "stg/erp_orders.sql":
        "CREATE TABLE stg.erp_orders (OrderId INT, CustomerId INT, Amount MONEY, etl_load_date DATETIME2)\n",
    "stg/crm_contacts.sql": "CREATE TABLE stg.crm_contacts (ContactId INT, CustomerId INT)\n",
    "stg/crm_leads.sql": "CREATE TABLE stg.crm_leads (LeadId INT)\n",
    "etl/vw_customers.sql":
        "CREATE VIEW etl.vw_customers AS SELECT CustomerId, UPPER(Name) AS Name FROM stg.erp_customers\n",
    "etl/vw_orders.sql":
        "CREATE VIEW etl.vw_orders AS SELECT OrderId, CustomerId, Amount FROM stg.erp_orders\n",
    "tmp/t01_customers.sql": "CREATE TABLE tmp.t01_customers (CustomerId INT, Name NVARCHAR(50))\n",
    "tmp/t02_orders.sql": "CREATE TABLE tmp.t02_orders (OrderId INT, CustomerId INT, Amount MONEY)\n",
    "dwh/dim_customer.sql":
        "CREATE TABLE dwh.dim_customer (customer_sk INT IDENTITY, CustomerId INT, Name NVARCHAR(50),\n"
        "    etl_load_date DATETIME2, etl_batch_id INT)\n",
    "dwh/fact_sales.sql":
        "CREATE TABLE dwh.fact_sales (OrderId INT, customer_sk INT, Amount MONEY, etl_load_date DATETIME2,\n"
        "    etl_batch_id INT)\n",
    "dwh/load.sql": """CREATE PROCEDURE dwh.load @batch INT AS
BEGIN
  INSERT INTO dwh.dim_customer (CustomerId, Name, etl_load_date, etl_batch_id)
  SELECT CustomerId, Name, GETDATE(), @batch FROM tmp.t01_customers;
  INSERT INTO dwh.fact_sales (OrderId, customer_sk, Amount, etl_load_date, etl_batch_id)
  SELECT o.OrderId, c.customer_sk, o.Amount, GETDATE(), @batch
  FROM tmp.t02_orders o JOIN dwh.dim_customer c ON c.CustomerId = o.CustomerId;
END
""",
    "pbi/customer.sql": "CREATE VIEW pbi.customer AS SELECT customer_sk, Name FROM dwh.dim_customer\n",
    "pbi/sales.sql": "CREATE VIEW pbi.sales AS SELECT OrderId, customer_sk, Amount FROM dwh.fact_sales\n",
    "cfg/load_settings.sql": "CREATE TABLE cfg.load_settings (Name NVARCHAR(50), Value NVARCHAR(50))\n",
    "audit/load_log.sql": "CREATE TABLE audit.load_log (Id INT, Message NVARCHAR(200))\n",
    "audit/write_log.sql":
        "CREATE PROCEDURE audit.write_log @m NVARCHAR(200) AS INSERT INTO audit.load_log (Message) VALUES (@m);\n",
}

# The answers a user gives to the schema questions of DWH.
ANSWERS = {"schema.tmp": "silver", "schema.dwh": "silver", "schema.etl": "silver", "schema.pbi": "gold"}


@pytest.fixture
def dwh(write_files, tmp_path):
    return write_files(DWH, str(tmp_path / "dwh"))


def _keys(draft) -> list[str]:
    return [q.key for q in draft.questions]


def _layers(draft) -> dict[str, str]:
    return {s.name: s.layer for s in draft.schemas}


# ---------------------------------------------------------------------------
# What the SQL and the names say
# ---------------------------------------------------------------------------

def test_layers_from_the_names_and_the_flows(dwh):
    draft = draft_config(dwh)
    assert _layers(draft) == {"stg": "bronze", "tmp": "bronze", "etl": "silver", "dwh": "silver",
                              "pbi": "gold", "audit": "ignore", "cfg": "ignore"}
    # A strong name the flows agree with, and a configuration schema no data flows
    # through, need no question.
    assert _keys(draft)[:4] == ["schema.tmp", "schema.dwh", "schema.etl", "schema.pbi"]
    assert not any(k in _keys(draft) for k in ("schema.stg", "schema.cfg", "schema.audit"))
    tmp = next(q for q in draft.questions if q.key == "schema.tmp")
    assert "nothing in the SQL loads its tables" in tmp.details[0] and tmp.proposed == "bronze"
    assert (draft.default_schema, draft.default_from) == ("dbo", "the .sqlproj")

    config = lineage_config.parse_config(draft.text)
    assert config.layers == (("bronze", ("stg", "tmp")), ("silver", ("dwh", "etl")), ("gold", ("pbi",)))
    assert config.ignore == ("audit", "cfg")


def test_the_answers_complete_the_draft(dwh):
    draft = draft_config(dwh, ANSWERS)
    assert draft.applied == 4 and draft.not_applied == []
    assert not any(k.startswith("schema.") for k in _keys(draft))
    # Silver now holds the staging tables nothing loads: one rule explains both,
    # their numbers generalized.
    rules = {r.name: r for r in draft.rules}
    assert (rules["tmp-from-etl"].match, rules["tmp-from-etl"].source) == \
        (r"tmp\.t\d{2}(?P<name>\w+)", "etl.vw{name}")
    assert rules["stg-source-systems"].external
    assert draft.technical == [r"etl_\w+", "customer_sk"]
    assert draft.targets == ["tmp.t01_customers", "tmp.t02_orders"] and draft.unexplained == []

    answers = {**ANSWERS, **{q.key: q.proposed for q in draft.questions}}
    final = draft_config(dwh, answers)
    assert final.questions == [] and final.text == draft.text


def test_the_saved_draft_describes_the_project(dwh):
    answers = {**ANSWERS, "rule.tmp-from-etl": "yes", "rule.stg-source-systems": "yes",
               "technical_columns": "yes"}
    draft = draft_config(dwh, answers)
    assert draft.questions == []
    lineage_tools.save_lineage_config(dwh, draft.text)
    lineage = load_sql_lineage(dwh)
    assert lineage.config.path.endswith("lineage.yaml")
    assert lineage.checks["loads_not_found"] == [] and lineage.checks["unassigned"] == {}
    assert lineage.checks["backward"] == [] and lineage.checks["columns_without_source"] == {}
    assert lineage.analysis.rules["tmp-from-etl"]["found"] == 2
    assert "Nothing to fix." in lineage_tools.test_lineage_config(dwh)


def test_the_power_bi_model_reads_the_last_layer(write_files, tmp_path):
    root = write_files(E2E_FILES, str(tmp_path / "ws"))
    draft = draft_config(os.path.join(root, "db"), project_path=os.path.join(root, "bi", "Shop.pbip"))
    assert draft.model == "Shop" and draft.existing.endswith("lineage.yaml")
    reads = {s.name: s.model_reads for s in draft.schemas}
    assert reads["mart"] == 2 and reads["core"] == 1
    question = next(q for q in draft.questions if q.key == "schema.core")
    assert "but the Power BI model reads it" in question.details[0]
    assert "Existing configuration:" in config_draft.format_draft(draft)


def _schema(name, upstream=(), downstream=(), model_reads=0) -> SchemaInfo:
    return SchemaInfo(name, {"table": 1}, set(upstream), set(downstream), model_reads=model_reads)


def test_placing_schemas_without_a_telling_name():
    schemas = {"a": _schema("a", downstream={"b"}), "b": _schema("b", {"a"}, {"c", "d"}),
               "c": _schema("c", {"b"}), "d": _schema("d", {"b"}, model_reads=2),
               "lonely": _schema("lonely")}
    config_draft._rank(schemas)
    config_draft._place(schemas, {}, set(), [])
    assert {k: (s.layer, s.sure) for k, s in schemas.items()} == {
        "a": ("bronze", False), "b": ("silver", False), "c": ("gold", False),
        "d": ("gold", True),                        # the model reads it, nothing after it
        "lonely": ("ignore", False)}


def test_a_name_the_flows_contradict_is_asked():
    schemas = {"mart": _schema("mart", downstream={"raw"}), "raw": _schema("raw", {"mart"})}
    config_draft._rank(schemas)
    config_draft._place(schemas, {}, set(), [])
    assert not schemas["raw"].sure and schemas["raw"].why == "name, but the flows disagree"
    assert not schemas["mart"].sure


@pytest.mark.parametrize("name, expected", [
    ("raw", ("bronze", True)), ("STG", ("bronze", True)), ("RawData", ("bronze", True)),
    ("00_bronze", ("bronze", True)), ("dwh", ("silver", False)), ("Mart", ("gold", True)),
    ("pbi", ("gold", False)), ("cfg", ("ignore", True)), ("raw_mart", ("", False)),
    ("uat_mart", ("", False)), ("sales", ("", False)),
])
def test_what_a_schema_name_says(name, expected):
    assert config_draft._word_layer(name) == expected


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

def test_answers_that_cannot_be_applied(dwh):
    draft = draft_config(dwh, {"schema.nope": "gold", "schema.tmp": "source", "rule.tmp-from-etl": "maybe",
                               "load.tmp.t01_customers": "etl.missing", "colour": "blue",
                               "layer_order": "gold, bronze", "technical_columns": "("})
    reasons = "\n".join(draft.not_applied)
    for expected in ("schema.nope: no table", "schema.tmp: 'source' is reserved",
                     "rule.tmp-from-etl: no rule of this draft has this name",
                     "load.tmp.t01_customers: 'etl.missing' is not another object", "colour: unknown key",
                     "layer_order: must list each layer once", "technical_columns: answer yes, no"):
        assert expected in reasons
    assert draft.applied == 0 and "schema.tmp" in _keys(draft) and "technical_columns" in _keys(draft)


def test_loads_described_by_the_user(dwh):
    draft = draft_config(dwh, {**ANSWERS, "load.[tmp].[t01_customers]": "stg.erp_customers",
                               "load.tmp.t02_orders": "external:erp.orders"})
    rules = {r.name: r for r in draft.rules}
    first = rules["load-tmp-t01-customers"]
    assert (first.match, first.source, first.external) == (r"tmp\.t01_customers", "stg.erp_customers", False)
    assert rules["load-tmp-t02-orders"].external and rules["load-tmp-t02-orders"].source == "erp.orders"
    assert "tmp-from-etl" not in rules            # both its tables are described by the user
    assert draft.text.index("load-tmp-t01-customers") < draft.text.index("stg-source-systems")
    assert draft.unexplained == []


def test_a_table_no_name_explains_is_asked(write_files, tmp_path):
    root = write_files(SHOP, str(tmp_path / "shop"))
    draft = draft_config(root)
    assert draft.unexplained == ["core.sales_history"] and "load.core.sales_history" in _keys(draft)
    question = next(q for q in draft.questions if q.key == "load.core.sales_history")
    assert "Columns: OrderId, CustomerKey, Amount, ValidFrom" in question.details
    closed = draft_config(root, {"load.core.sales_history": "none"})
    assert "load.core.sales_history" not in _keys(closed) and closed.unexplained == ["core.sales_history"]
    loaded = draft_config(root, {"load.core.sales_history": "core.sales"})
    assert loaded.unexplained == [] and loaded.rules[0].source == "core.sales"


def test_a_new_layer_and_its_order(dwh):
    answers = {**ANSWERS, "schema.dwh": "gold", "schema.pbi": "semantic"}
    draft = draft_config(dwh, answers)
    assert [layer for layer, _schemas in draft.layers] == ["bronze", "silver", "gold", "semantic"]
    question = next(q for q in draft.questions if q.key == "layer_order")
    assert question.proposed == "bronze, silver, gold, semantic"
    reordered = draft_config(dwh, {**answers, "layer_order": ["bronze", "silver", "semantic", "gold"]})
    assert [layer for layer, _schemas in reordered.layers] == ["bronze", "silver", "semantic", "gold"]
    assert "layer_order" not in _keys(reordered)


def test_default_schema(write_files, tmp_path):
    files = {"P.sqlproj": SQLPROJ.replace(">dbo<", ">core<"),
             "a.sql": "CREATE TABLE bronze.items (id INT)\nGO\nCREATE VIEW silver.items AS SELECT id FROM bronze.items\n"}
    root = write_files(files, str(tmp_path / "p"))
    assert draft_config(root).default_schema == "core"
    answered = draft_config(root, {"default_schema": "[silver]"})
    assert (answered.default_schema, answered.default_from) == ("silver", "answer")
    assert "default_schema: silver" in answered.text


# ---------------------------------------------------------------------------
# Columns the ETL creates
# ---------------------------------------------------------------------------

TECH = {
    "P.sqlproj": SQLPROJ,
    "raw.sql": "CREATE TABLE raw.items (id INT, name NVARCHAR(10))\n",
    "core.sql": "CREATE TABLE core.items (id INT, dw_name NVARCHAR(10), dw_load_date DATETIME2, dw_batch_id INT)\n"
                "GO\nCREATE PROCEDURE core.load @b INT AS INSERT INTO core.items (id, dw_name, dw_load_date, "
                "dw_batch_id) SELECT id, name, GETDATE(), @b FROM raw.items\n"
                "GO\nCREATE TABLE core.parts (id INT, dw_load_date DATETIME2, dw_batch_id INT)\n"
                "GO\nCREATE PROCEDURE core.load_parts @b INT AS INSERT INTO core.parts (id, dw_load_date, "
                "dw_batch_id) SELECT id, GETDATE(), @b FROM raw.items\n",
    "mart.sql": "\nGO\n".join(f"CREATE VIEW mart.v{i} AS SELECT id, 'N/A' AS Origin FROM core.items"
                              for i in range(3)) + "\nGO\nCREATE VIEW mart.w AS SELECT id, 0 AS Flag FROM core.parts\n",
}


def test_technical_columns(write_files, tmp_path):
    root = write_files(TECH, str(tmp_path / "t"))
    draft = draft_config(root)
    # 'dw_\w+' would also cover dw_name, which a source fills: the names stay one by one.
    # 'Origin' has no source in three views; 'Flag' only in one.
    assert draft.technical == ["dw_batch_id", "dw_load_date", "Origin"]
    assert draft_config(root, {"technical_columns": "no"}).technical == []
    listed = draft_config(root, {"technical_columns": r"dw_(load|batch)_\w+, Origin\d{0,2}"})
    assert listed.technical == [r"dw_(load|batch)_\w+", r"Origin\d{0,2}"]
    assert draft_config(root, {"technical_columns": ["Origin"]}).technical == ["Origin"]


@pytest.mark.parametrize("name, technical", [
    ("etl_loaded_at", True), ("LoadDate", True), ("CustomerSK", True), ("customer_sk", True),
    ("ValidFrom", True), ("BatchId", True), ("RowHash", True), ("created_at", True),
    ("desk", False), ("Amount", False), ("SystemName", False), ("CustomerKey", False),
])
def test_what_a_column_name_says(name, technical):
    assert config_draft._looks_technical(name) is technical


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

def test_the_draft_tool(dwh, monkeypatch):
    text = lineage_tools.draft_lineage_config(dwh)
    assert text.startswith("=== LINEAGE CONFIGURATION DRAFT: Dwh ===")
    assert "Open questions: 6." in text and "1. schema.tmp: Which layer is schema 'tmp' in?" in text
    assert "```yaml\n# lineage.yaml of the database project 'Dwh'." in text
    # Answers as JSON text, with a boolean, as some clients send them.
    answered = lineage_tools.draft_lineage_config(dwh, json.dumps({**ANSWERS, "rule.stg-source-systems": False}))
    assert "Answers applied: 5." in answered and "stg-source-systems" not in answered.split("```yaml")[1]
    with pytest.raises(ValueError, match="must be an object"):
        lineage_tools.draft_lineage_config(dwh, "not json")
    # Too long: the schema table goes first, then the details; the file stays whole.
    monkeypatch.setattr(lineage_tools, "MAX_TOOL_OUTPUT_CHARS", len(text) - 200)
    reduced = lineage_tools.draft_lineage_config(dwh)
    assert "Schemas, in flow order" not in reduced and reduced.count("```") == 2
    assert "technical_columns: [" in reduced


def test_nothing_to_ask(write_files, tmp_path):
    files = {"P.sqlproj": SQLPROJ, "a.sql": "CREATE TABLE bronze.items (id INT)\nGO\n"
             "CREATE VIEW silver.items AS SELECT id FROM bronze.items\nGO\n"
             "CREATE VIEW gold.items AS SELECT id FROM silver.items\n"}
    root = write_files(files, str(tmp_path / "p"))
    draft = draft_config(root)
    assert draft.questions == [] and draft.rules == [] and draft.technical == [] and draft.ignore == []
    assert "No open question: the file below is complete." in lineage_tools.draft_lineage_config(root)


def test_a_project_without_data(write_files, tmp_path):
    root = write_files({"P.sqlproj": SQLPROJ, "a.sql": "SELECT 1\n"}, str(tmp_path / "p"))
    with pytest.raises(LookupError, match="nothing to configure"):
        draft_config(root)


def test_saving_the_configuration(dwh, tmp_path, monkeypatch):
    draft = draft_config(dwh)
    mine = os.path.join(str(user_folder.default_folder()), "projects", "dwh", "Dwh.lineage.yaml")
    with pytest.raises(ValueError, match="Not saved"):
        lineage_tools.save_lineage_config(dwh, "layers: [raw]\n")
    with pytest.raises(ValueError, match="empty"):
        lineage_tools.save_lineage_config(dwh, "  ")
    assert not os.path.exists(mine) and user_folder.needs_choice()

    # By default it is yours, outside the repository; the first save says where.
    message = lineage_tools.save_lineage_config(dwh, f"```yaml\n{draft.text}```")
    assert message.startswith(f"Saved '{mine}': yours, outside the repository.")
    assert "First use" in message
    # The assistant is told to warn the user: a generated file is only a draft.
    assert "Tell the user, as it is: WARNING:" in message and "--lineage-config" in message
    assert message.index("WARNING") < message.index("First use") < message.index("Next:")
    assert "With this file the lineage has" in message
    with open(mine, encoding="utf-8") as handle:
        assert handle.read() == draft.text
    assert not os.path.exists(os.path.join(dwh, "lineage.yaml")), "the repository is not touched"
    assert load_sql_lineage(dwh).config.path == mine
    assert "(yours, outside the repository)" in lineage_tools.test_lineage_config(dwh)
    with pytest.raises(FileExistsError):
        lineage_tools.save_lineage_config(dwh, draft.text)
    assert "(replaced)" in lineage_tools.save_lineage_config(dwh, draft.text, overwrite=True)

    # The repository's, only when asked: it wins over yours, and both say so.
    shared = os.path.join(dwh, "lineage.yaml")
    message = lineage_tools.save_lineage_config(dwh, draft.text, scope="repo")
    assert message.startswith(f"Saved '{shared}': the repository's, shared with the team.")
    assert load_sql_lineage(dwh).config.path == shared
    assert "is not used: the repository's wins" in lineage_tools.test_lineage_config(dwh)
    message = lineage_tools.save_lineage_config(dwh, draft.text, overwrite=True)
    assert "the repository has its own" in message
    with pytest.raises(ValueError, match="'scope'"):
        lineage_tools.save_lineage_config(dwh, draft.text, scope="team")

    # MCP_ALLOWED_ROOTS bounds the repository; your own folder is always yours.
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path / "elsewhere"))
    with pytest.raises(ValueError, match="outside the folders"):
        lineage_tools.save_lineage_config(dwh, draft.text, overwrite=True, scope="repo")

# ---------------------------------------------------------------------------
# The warning of a generated file, shown to the user as it is
# ---------------------------------------------------------------------------

def _save_through_client(dwh, content, answers=(), asked=None, mode="auto", **arguments):
    """Calls save_lineage_config through an in-process client; `answers` play the user filling the forms."""
    import asyncio

    from mcp import Client
    from mcp.server.mcpserver import MCPServer
    queue = list(answers)

    async def callback(_context, params):
        if asked is not None:
            asked.append(params.message)
        return queue.pop(0)

    async def run():
        mcp = MCPServer("review")
        lineage_tools.register_lineage_writer_tools(mcp)
        async with Client(mcp, mode=mode, elicitation_callback=callback if answers else None) as client:
            result = await client.call_tool(
                "save_lineage_config", {"sql_project_path": dwh, "content": content, **arguments})
            return result.is_error, "\n".join(b.text for b in result.content if hasattr(b, "text"))
    return asyncio.run(run())


def _review_answer(open_it=True):
    from mcp_types import ElicitResult
    return ElicitResult(action="accept", content={"open_in_editor": open_it})


@pytest.fixture
def editor(monkeypatch):
    """The editor is not opened for real: the paths it was asked to open."""
    from mcp_data_platform_toolkit import rules_cli
    opened = []
    monkeypatch.setattr(rules_cli, "open_in_editor", lambda path: opened.append(path) or True)
    user_folder.choose_folder(user_folder.default_folder())
    return opened


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_a_generated_file_shows_the_warning_in_a_form_and_opens_the_editor(dwh, editor, mode):
    """Both ways a client talks to the server: the form reaches the user the same."""
    from mcp_data_platform_toolkit.lineage import config_review
    draft = draft_config(dwh, ANSWERS)
    asked: list = []
    error, text = _save_through_client(dwh, draft.text, [_review_answer()], asked, mode=mode)
    assert not error and text.startswith("Saved ")
    assert asked == [config_review.review_message()] and config_review.DRAFT_WARNING in asked[0]
    assert "The user was shown the warning" in text and "Tell the user" not in text
    mine = lineage_config.personal_config_file(dwh, os.path.join(dwh, "Dwh.sqlproj"))
    assert [str(path) for path in editor] == [mine] and os.path.isfile(mine)
    assert "With this file the lineage has" in text


def test_the_editor_stays_closed_when_the_user_unchecks_it(dwh, editor):
    error, text = _save_through_client(dwh, draft_config(dwh, ANSWERS).text, [_review_answer(False)])
    assert not error and text.startswith("Saved ") and editor == []


@pytest.mark.parametrize("answer", ["decline", "cancel"])
def test_declining_the_form_still_saves_and_the_assistant_passes_the_warning_on(dwh, editor, answer):
    from mcp_types import ElicitResult

    from mcp_data_platform_toolkit.lineage import config_review
    error, text = _save_through_client(dwh, draft_config(dwh, ANSWERS).text, [ElicitResult(action=answer)])
    mine = lineage_config.personal_config_file(dwh, os.path.join(dwh, "Dwh.sqlproj"))
    assert not error and text.startswith("Saved ") and os.path.isfile(mine)
    assert f"Tell the user, as it is: {config_review.DRAFT_WARNING}" in text, "they may not have seen it"
    assert editor == []


def test_a_file_written_by_hand_gets_no_warning_and_no_form(dwh, editor):
    asked: list = []
    by_hand = "version: 1\nlayers:\n  bronze: [stg, tmp]\n  silver: [dwh, etl]\n  gold: pbi\nignore: [audit, cfg]\n"
    error, text = _save_through_client(dwh, by_hand, [_review_answer()], asked)
    assert not error and text.startswith("Saved ") and asked == [] and editor == []
    assert "WARNING" not in text and "With this file the lineage has" in text


def test_the_answer_says_when_the_file_leaves_out_the_lineage(dwh, editor):
    error, text = _save_through_client(dwh, "version: 1\nlayers:\n  bronze: nothing\nignore: [stg, tmp, etl, dwh, pbi]\n")
    assert not error and "ATTENTION: it leaves out" in text and "mostly empty" in text


def test_a_generated_file_without_forms_carries_the_warning_for_the_assistant(dwh, editor):
    from mcp_data_platform_toolkit.lineage import config_review
    error, text = _save_through_client(dwh, draft_config(dwh, ANSWERS).text)
    assert not error and f"Tell the user, as it is: {config_review.DRAFT_WARNING}" in text and editor == []


def test_the_review_is_never_a_parameter_the_model_fills():
    import asyncio

    from mcp import Client
    from mcp.server.mcpserver import MCPServer

    async def schema():
        mcp = MCPServer("review")
        lineage_tools.register_lineage_writer_tools(mcp)
        async with Client(mcp) as client:
            tools = (await client.list_tools()).tools
            return next(t for t in tools if t.name == "save_lineage_config").input_schema
    assert "review" not in asyncio.run(schema()).get("properties", {})


# ---------------------------------------------------------------------------
# A single schema, and what the SQL reads from outside the project
# ---------------------------------------------------------------------------

ONE_SCHEMA = {
    "P.sqlproj": SQLPROJ,
    "t.sql": "CREATE TABLE dbo.stg_orders (id INT, amount MONEY)\nGO\n"
             "CREATE TABLE dbo.fact_sales (id INT, amount MONEY)\nGO\n"
             "CREATE PROCEDURE dbo.usp_load AS INSERT INTO dbo.fact_sales (id, amount) "
             "SELECT id, amount FROM dbo.stg_orders\nGO\n"
             "CREATE VIEW dbo.vw_sales AS SELECT id, amount FROM dbo.fact_sales\n",
}


def test_a_schema_with_flows_only_between_its_own_objects_is_not_ignored(write_files, tmp_path):
    root = write_files(ONE_SCHEMA, str(tmp_path / "p"))
    draft = draft_config(root)
    assert draft.ignore == [] and _layers(draft) == {"dbo": "bronze"}
    (question,) = draft.questions
    assert question.key == "schema.dbo" and question.proposed == "bronze"
    assert "forms a single layer" in question.details[0] and "ignoring it would leave out" in question.details[0]
    # Drafted, the configuration keeps every object and every link.
    config = tmp_path / "draft.yaml"
    config.write_text(draft.text, encoding="utf-8")
    lineage = load_sql_lineage(root, str(config))
    assert set(lineage.objects.nodes) == {"dbo.stg_orders", "dbo.fact_sales", "dbo.vw_sales"}
    assert len(lineage.objects.edges) == 2


def test_a_schema_without_any_flow_is_still_ignored(write_files, tmp_path):
    files = {**ONE_SCHEMA, "cfg.sql": "CREATE TABLE cfg.settings (k NVARCHAR(10), v NVARCHAR(10))\n"}
    draft = draft_config(write_files(files, str(tmp_path / "p")))
    assert draft.ignore == ["cfg"] and _layers(draft) == {"dbo": "bronze", "cfg": "ignore"}


SYNONYM = {
    "P.sqlproj": SQLPROJ,
    "t.sql": "CREATE TABLE dwh.dim_order (id INT, amount MONEY)\nGO\n"
             "CREATE PROCEDURE dwh.load AS BEGIN SELECT id, amount INTO #t FROM stg.erp_orders;\n"
             "INSERT INTO dwh.dim_order (id, amount) SELECT id, amount FROM #t; END\nGO\n"
             "CREATE VIEW pbi.orders AS SELECT id, amount FROM dwh.dim_order\n",
}


def test_what_the_sql_reads_outside_the_project_is_a_source_and_the_draft_says_so(write_files, tmp_path):
    root = write_files(SYNONYM, str(tmp_path / "p"))
    draft = draft_config(root)
    assert draft.outside == ["stg.erp_orders"]
    assert "stg.erp_orders" in draft.text and "shown as sources" in draft.text
    assert "stg.erp_orders" in config_draft.format_draft(draft)
    config = tmp_path / "draft.yaml"
    config.write_text(draft.text, encoding="utf-8")
    nodes = load_sql_lineage(root, str(config)).objects.nodes
    assert nodes["stg.erp_orders"].layer == "source" and nodes["dwh.dim_order"].layer == "silver"
    parse = lineage_config.parse_config(draft.text)
    assert "stg" not in [s for _name, schemas in parse.layers for s in schemas]


def test_an_object_outside_the_project_in_a_schema_of_a_layer_keeps_that_layer(write_files, tmp_path):
    root = write_files(SYNONYM, str(tmp_path / "p"))
    config = tmp_path / "mine.yaml"
    config.write_text("version: 1\nlayers:\n  bronze: stg\n  silver: dwh\n  gold: pbi\n", encoding="utf-8")
    assert load_sql_lineage(root, str(config)).objects.nodes["stg.erp_orders"].layer == "bronze"


# ---------------------------------------------------------------------------
# What is a draft: the file says 'draft: true' until the user checked it
# ---------------------------------------------------------------------------

def _without(text: str, *fragments: str) -> str:
    return "\n".join(line for line in text.splitlines() if not any(f in line for f in fragments))


def test_a_draft_says_so_and_stays_a_draft_whoever_edits_it(dwh):
    from mcp_data_platform_toolkit.lineage import draft_notice
    text = draft_config(dwh, ANSWERS).text
    assert "\ndraft: true\n" in text and lineage_config.parse_config(text).draft
    # Without its comments, reformatted, changed by the assistant: still a draft.
    edited = _without(text, "#").replace("gold: pbi", "gold: [pbi, cfg]")
    assert draft_notice.is_generated(f"```yaml\n{edited}\n```")


def test_a_checked_or_hand_written_file_is_not_a_draft(dwh):
    from mcp_data_platform_toolkit.lineage import draft_notice
    text = draft_config(dwh, ANSWERS).text
    assert not draft_notice.is_generated(_without(text, "draft: true"))
    assert not draft_notice.is_generated(text.replace("draft: true", "draft: false"))
    assert not draft_notice.is_generated("version: 1\nlayers:\n  bronze: raw\n")
    assert not draft_notice.is_generated("# draft: true in a comment\nversion: 1\n")
    assert not draft_notice.is_generated("not: [yaml") and not draft_notice.is_generated("")


def test_draft_must_be_true_or_false():
    with pytest.raises(lineage_config.ConfigError, match="draft: must be true"):
        lineage_config.parse_config("version: 1\ndraft: yes please\n")


def test_every_lineage_answer_says_the_configuration_is_a_draft(dwh, tmp_path):
    from mcp_data_platform_toolkit.lineage import draft_notice
    text = draft_config(dwh, ANSWERS).text
    config = tmp_path / "lineage.yaml"
    config.write_text(text, encoding="utf-8")
    for answer in (lineage_tools.test_lineage_config(dwh, str(config)),
                   lineage_tools.get_sql_project_lineage(dwh, config_path=str(config))):
        assert draft_notice.DRAFT_IN_USE in answer
    config.write_text(_without(text, "draft: true"), encoding="utf-8")
    assert draft_notice.DRAFT_IN_USE not in lineage_tools.test_lineage_config(dwh, str(config))


# ---------------------------------------------------------------------------
# The summary of a save
# ---------------------------------------------------------------------------

def test_the_summary_reads_the_sql_only_when_the_file_has_rules(dwh, monkeypatch, tmp_path):
    """Without rules the cached lineage read without configuration is seen through the layers; with them, a full analysis."""
    from mcp_data_platform_toolkit.sql_project import project as sql_project
    calls = []
    real = sql_project._analyze
    monkeypatch.setattr(sql_project, "_analyze", lambda *a, **k: calls.append(1) or real(*a, **k))
    without_rules = "version: 1\nlayers:\n  bronze: [stg, tmp]\n  silver: [dwh, etl]\n  gold: pbi\nignore: [audit, cfg]\n"
    lineage_tools._lineage_with(dwh, "x.yaml", without_rules)
    first = len(calls)
    lineage_tools._lineage_with(dwh, "x.yaml", without_rules)
    assert len(calls) == first, "cached"
    full = lineage_tools._lineage_with(dwh, "x.yaml", without_rules)
    with_rules = without_rules + "rules:\n  - name: r\n    match: 'tmp\\.t01_customers'\n    from: 'etl.vw_customers'\n"
    saved = tmp_path / "saved.yaml"
    saved.write_text(with_rules, encoding="utf-8")
    before = len(calls)
    ruled = lineage_tools._lineage_with(dwh, str(saved), with_rules)
    assert len(calls) > before and ruled.config.rules
    assert full.checks["by_layer"].keys() == ruled.checks["by_layer"].keys()


def test_an_error_in_the_summary_does_not_undo_the_save(dwh, monkeypatch):
    monkeypatch.setattr(lineage_tools, "_lineage_with", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    error, text = _save_through_client(dwh, "version: 1\nlayers:\n  bronze: stg\n")
    mine = lineage_config.personal_config_file(dwh, os.path.join(dwh, "Dwh.sqlproj"))
    assert not error and text.startswith("Saved ") and os.path.isfile(mine)
    assert "RuntimeError: boom" in text and "run test_lineage_config" in text


# ---------------------------------------------------------------------------
# Opening the editor
# ---------------------------------------------------------------------------

class _Process:
    def __init__(self, outcome):
        self.outcome = outcome

    def wait(self, timeout=None):
        import subprocess
        if self.outcome == "running":
            raise subprocess.TimeoutExpired("opener", timeout)
        return self.outcome


@pytest.mark.parametrize("outcome, opened", [("running", True), (0, True), (3, False)])
def test_the_editor_is_not_waited_for(tmp_path, monkeypatch, outcome, opened):
    import subprocess
    import sys

    from mcp_data_platform_toolkit import rules_cli
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Process(outcome))
    assert rules_cli.open_in_editor(tmp_path / "f.yaml") is opened


def test_without_a_graphical_session_or_an_opener_the_editor_is_not_opened(tmp_path, monkeypatch):
    import subprocess
    import sys

    from mcp_data_platform_toolkit import rules_cli
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a) or _Process(0))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert rules_cli.open_in_editor(tmp_path / "f.yaml") is False and started == []
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("xdg-open")))
    assert rules_cli.open_in_editor(tmp_path / "f.yaml") is False


# ---------------------------------------------------------------------------
# The draft never throws the lineage away: across different warehouses
# ---------------------------------------------------------------------------

WAREHOUSES = {
    "medallion": {
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE bronze.orders (id INT, amount MONEY)\nGO\n"
                 "CREATE VIEW silver.orders AS SELECT id, amount FROM bronze.orders\nGO\n"
                 "CREATE VIEW gold.sales AS SELECT id, amount FROM silver.orders\nGO\n"
                 "CREATE TABLE audit.load_log (run_id INT)\n"},
    "one_schema": ONE_SCHEMA,
    "synonym": SYNONYM,
    "opaque_names": {
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE intake.orders (id INT, amount MONEY)\nGO\n"
                 "CREATE VIEW depot.orders AS SELECT id, amount FROM intake.orders\nGO\n"
                 "CREATE VIEW report.sales AS SELECT id, amount FROM depot.orders\n"},
    "generic_loader": {
        "P.sqlproj": SQLPROJ,
        "a.sql": "CREATE TABLE src.customers (id INT)\nGO\nCREATE TABLE dwh.dim_customer (id INT)\nGO\n"
                 "CREATE PROCEDURE etl.load @s SYSNAME AS EXEC sp_executesql @s\nGO\n"
                 "CREATE VIEW pbi.customers AS SELECT id FROM dwh.dim_customer\n"},
}


@pytest.mark.parametrize("name", sorted(WAREHOUSES))
def test_accepting_every_proposed_answer_never_leaves_the_lineage_empty(write_files, tmp_path, name):
    """The invariant that matters: the proposed answers may be approximate, never destructive."""
    from mcp_data_platform_toolkit.sql_project.project import load_unconfigured_lineage
    root = write_files(WAREHOUSES[name], str(tmp_path / "p"))
    draft = draft_config(root)
    draft = draft_config(root, {q.key: q.proposed for q in draft.questions})
    base = load_unconfigured_lineage(root, draft.default_schema)
    config = tmp_path / "draft.yaml"
    config.write_text(draft.text, encoding="utf-8")
    lineage = load_sql_lineage(root, str(config))

    ignored = {s.lower() for s in draft.ignore}
    for info in draft.schemas:
        if info.layer == "ignore" and info.why == "no data flow":
            touching = [e for e in base.objects.edges
                        if info.name.lower() in (e.source.split(".")[0].lower(), e.target.split(".")[0].lower())]
            assert touching == [], f"'{info.name}' is ignored for having no flow, but has {touching}"
    data = {k for k, o in base.analysis.objects.items() if o.in_project and o.kind in DATA_KINDS
            and o.schema.lower() not in ignored}
    assert data, "the warehouse has objects outside the ignored schemas"
    assert data <= set(lineage.objects.nodes), "every object outside an ignored schema stays in the lineage"
    assert all(lineage.objects.nodes[k].layer for k in data), "and in a layer"


# ---------------------------------------------------------------------------
# The items the summary of a save lists
# ---------------------------------------------------------------------------

GOOD = "version: 1\nlayers:\n  bronze: [stg, tmp]\n  silver: [dwh, etl]\n  gold: pbi\nignore: [audit, cfg]\n"
CONFIGS = {
    "only_one_layer": "version: 1\nlayers:\n  bronze: stg\n",
    "rule_matching_nothing": GOOD + "rules:\n  - name: nope\n    match: 'zzz\\..*'\n    from: 'etl.vw_customers'\n",
    "backwards": "version: 1\nlayers:\n  bronze: pbi\n  silver: [dwh, etl, tmp]\n  gold: stg\nignore: [audit, cfg]\n",
    "good": GOOD,
}


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_the_items_of_a_save_are_the_ones_test_lineage_config_counts(dwh, tmp_path, name):
    """The summary of a save and the check of the file must not drift apart."""
    import re
    config = tmp_path / f"{name}.yaml"
    config.write_text(CONFIGS[name], encoding="utf-8")
    lineage = load_sql_lineage(dwh, str(config))
    items = lineage_tools._attention_items(lineage)
    report = lineage_tools.test_lineage_config(dwh, str(config))
    found = re.search(r"(\d+) item\(s\) marked \[!\]", report)
    assert len(items) == (int(found.group(1)) if found else 0), (items, report)
    if name != "good":
        assert items


def test_the_answer_of_a_save_lists_what_to_look_at(dwh, editor):
    error, text = _save_through_client(dwh, CONFIGS["only_one_layer"])
    assert not error and "item(s) to look at:" in text and "schemas in no layer" in text
    error, text = _save_through_client(dwh, CONFIGS["good"], overwrite=True)
    assert not error and "item(s) to look at" not in text
    assert "finds nothing to fix" in text and "the right ones" in text


# ---------------------------------------------------------------------------
# Patterns of real warehouses (WideWorldImportersDW, AdventureWorksDW)
# ---------------------------------------------------------------------------

# Staging loaded from outside the SQL, taking the keys of the dimensions
# (lookups: one column each), then loading the dimensions and the facts.
STAGING_WITH_LOOKUPS = {
    "P.sqlproj": SQLPROJ,
    "t.sql": "CREATE TABLE Integration.City_Staging ([WWI City ID] INT, City NVARCHAR(50), Region NVARCHAR(50),"
             " Country NVARCHAR(50))\nGO\n"
             "CREATE TABLE Integration.Sale_Staging ([WWI Invoice ID] INT, [WWI City ID] INT, [City Key] INT,"
             " Quantity INT)\nGO\n"
             "CREATE TABLE Integration.Order_Staging ([WWI Order ID] INT, [WWI City ID] INT, [City Key] INT,"
             " Quantity INT)\nGO\n"
             "CREATE TABLE Dimension.City ([City Key] INT IDENTITY, [WWI City ID] INT, City NVARCHAR(50),"
             " Region NVARCHAR(50), Country NVARCHAR(50))\nGO\n"
             "CREATE TABLE Fact.Sale ([City Key] INT, [WWI Invoice ID] INT, Quantity INT)\nGO\n"
             "CREATE TABLE Fact.[Order] ([City Key] INT, [WWI Order ID] INT, Quantity INT)\nGO\n",
    "p.sql": """CREATE PROCEDURE Integration.MigrateStagedData AS
BEGIN
    INSERT Dimension.City ([WWI City ID], City, Region, Country)
    SELECT [WWI City ID], City, Region, Country FROM Integration.City_Staging;
    UPDATE s SET s.[City Key] = c.[City Key]
    FROM Integration.Sale_Staging AS s INNER JOIN Dimension.City AS c ON c.[WWI City ID] = s.[WWI City ID];
    UPDATE o SET o.[City Key] = c.[City Key]
    FROM Integration.Order_Staging AS o INNER JOIN Dimension.City AS c ON c.[WWI City ID] = o.[WWI City ID];
    INSERT Fact.Sale ([City Key], [WWI Invoice ID], Quantity)
    SELECT [City Key], [WWI Invoice ID], Quantity FROM Integration.Sale_Staging;
    INSERT Fact.[Order] ([City Key], [WWI Order ID], Quantity)
    SELECT [City Key], [WWI Order ID], Quantity FROM Integration.Order_Staging;
END
""",
}


def test_lookups_are_told_apart_from_loads_by_the_columns_they_move(write_files, tmp_path):
    """More tables take keys from the dimension than the dimension loads from: the columns decide."""
    draft = draft_config(write_files(STAGING_WITH_LOOKUPS, str(tmp_path / "p")))
    by_name = {info.name: info for info in draft.schemas}
    assert by_name["Integration"].lookups == {"dimension"} and by_name["Integration"].upstream == set()
    assert by_name["Dimension"].upstream == {"integration"}
    assert _layers(draft) == {"Integration": "bronze", "Dimension": "gold", "Fact": "gold"}
    question = next(q for q in draft.questions if q.key == "schema.integration")
    assert "its name suggests silver, but nothing in the SQL loads it" in question.details[0]
    assert "taken as lookups" in question.details[0]


def test_integration_is_a_guess_never_a_certainty():
    assert config_draft._word_layer("Integration") == ("silver", False)
    assert config_draft._word_layer("Dimension") == ("gold", False)


# A star schema all in dbo, loaded from outside the SQL, with views over it.
STAR_IN_DBO = {
    "P.sqlproj": SQLPROJ,
    "t.sql": "\nGO\n".join(f"CREATE TABLE dbo.{n} ({n}Key INT, Name NVARCHAR(50))"
                           for n in ("DimCustomer", "DimProduct", "DimDate", "FactInternetSales",
                                     "FactResellerSales", "DatabaseLog"))
             + "\nGO\nCREATE VIEW dbo.vTargetMail AS SELECT DimCustomerKey, Name FROM dbo.DimCustomer\n",
}


def test_a_schema_named_after_nothing_takes_the_layer_its_objects_are_named_after(write_files, tmp_path):
    draft = draft_config(write_files(STAR_IN_DBO, str(tmp_path / "p")))
    assert _layers(draft) == {"dbo": "gold"}
    (question,) = draft.questions
    assert question.proposed == "gold"
    assert "5 of its 7 objects are named like gold (DimCustomer, DimDate, ...)" in question.details[0]


def test_objects_named_like_different_layers_in_one_schema_are_explained(write_files, tmp_path):
    draft = draft_config(write_files(ONE_SCHEMA, str(tmp_path / "p")))
    (question,) = draft.questions
    assert "named like different layers (bronze: 1 (stg_orders), gold: 1 (fact_sales))" in question.details[0]
    assert "need a schema of their own" in question.details[0]
    assert question.proposed != "ignore"


WAREHOUSES["staging_with_lookups"] = STAGING_WITH_LOOKUPS
WAREHOUSES["star_in_dbo"] = STAR_IN_DBO


@pytest.mark.parametrize("name", ["staging_with_lookups", "star_in_dbo"])
def test_the_real_patterns_never_leave_the_lineage_empty_either(write_files, tmp_path, name):
    test_accepting_every_proposed_answer_never_leaves_the_lineage_empty(write_files, tmp_path, name)


def test_the_check_takes_lookups_as_lookups_and_real_backward_flows_as_backward(write_files, tmp_path):
    """The draft and test_lineage_config judge flows both ways the same way."""
    root = write_files(STAGING_WITH_LOOKUPS, str(tmp_path / "p"))
    config = tmp_path / "lineage.yaml"
    config.write_text("version: 1\nlayers:\n  bronze: Integration\n  gold: [Dimension, Fact]\n", encoding="utf-8")
    checks = load_sql_lineage(root, str(config)).checks
    assert checks["backward"] == [] and len(checks["lookups"]) == 2
    report = lineage_tools.test_lineage_config(root, str(config))
    assert "Flows going backwards" not in report and "Lookups (2)" in report and "Nothing to fix." in report
    # A view of the first layer reading the last one builds data from it: backwards, not a lookup.
    files = {**STAGING_WITH_LOOKUPS,
             "v.sql": "CREATE VIEW Integration.v AS SELECT [City Key], City FROM Dimension.City\n"}
    root = write_files(files, str(tmp_path / "q"))
    checks = load_sql_lineage(root, str(config)).checks
    assert any(line.startswith("Dimension.City [gold] -> Integration.v [bronze]") for line in checks["backward"])
