"""
A database project with no lineage configuration: the lineage tools infer its
structure from the SQL, show it to the user and save it, as the user's own, only
when the user says yes. And a view over files that a pipeline writes from
another object of the project is no longer a silent dead end of the lineage.

The warehouse is synthetic: stg (files of the source system) -> etl (views) ->
Parquet files written by a pipeline -> dwh (views over the Parquet) -> pbi.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp_types import ElicitResult

from mcp_data_platform_toolkit.lineage import lineage_tools, structure
from mcp_data_platform_toolkit.lineage.suggestions import _common, suggest_rules
from mcp_data_platform_toolkit.sql_project import project as sql_project
from mcp_data_platform_toolkit.sql_project.config import personal_config_path

ENTITIES = ("stocks", "sales")


def _openrowset(path: str, columns: str) -> str:
    return (f"SELECT t.* FROM OPENROWSET(BULK '{path}', DATA_SOURCE='adls', FORMAT='PARQUET') "
            f"WITH ({columns}) AS t")


def _warehouse() -> dict:
    builds = ["stg\\Views\\erp_Ledger.sql", "etl\\Views\\vw_T040_deposit.sql"]
    files = {
        "stg/Views/erp_Ledger.sql":
            "CREATE VIEW stg.erp_Ledger AS " + _openrowset("erp/Ledger/Ledger.parquet",
                                                           "ItemNo varchar(20), Quantity decimal(18,4)"),
        "etl/Views/vw_T040_deposit.sql":
            "CREATE VIEW etl.vw_T040_deposit AS SELECT l.ItemNo, l.Quantity * 2.5 AS amount FROM stg.erp_Ledger l",
    }
    for entity in ENTITIES:
        files[f"etl/Views/vw_fact_{entity}.sql"] = (
            f"CREATE VIEW etl.vw_fact_{entity} AS SELECT d.ItemNo, d.amount FROM etl.vw_T040_deposit d")
        files[f"dwh/Views/fact_{entity}.sql"] = (
            f"CREATE VIEW dwh.fact_{entity} AS "
            + _openrowset(f"common/fact/{entity}/fact_{entity}.parquet", "ItemNo varchar(20), amount float"))
        files[f"pbi/Views/vw_fact_{entity}.sql"] = (
            f"CREATE VIEW pbi.vw_fact_{entity} AS SELECT s.ItemNo, s.amount FROM dwh.fact_{entity} s")
        builds += [f"etl\\Views\\vw_fact_{entity}.sql", f"dwh\\Views\\fact_{entity}.sql",
                   f"pbi\\Views\\vw_fact_{entity}.sql"]
    items = "".join(f'<Build Include="{b}" />' for b in builds)
    files["Wh.sqlproj"] = ('<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/'
                           f'msbuild/2003"><ItemGroup>{items}</ItemGroup></Project>')
    return files


@pytest.fixture
def warehouse(write_files, tmp_path):
    """The folder of the database project, inside a Git repository called 'repo'."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    return write_files(_warehouse(), str(tmp_path / "repo" / "db"))


def _listing(folder: str) -> list[str]:
    return sorted(os.path.join(d, f) for d, _s, fs in os.walk(folder) for f in fs)


def _own_file(folder: str) -> str:
    return personal_config_path(folder, os.path.join(folder, "Wh.sqlproj"))


COLUMN = {"target": "pbi.vw_fact_sales.amount", "direction": "upstream", "level": "column"}


def _answer(decision: str, changes: str = "") -> ElicitResult:
    return ElicitResult(action="accept", content={"decision": decision, "changes": changes})


def _call(tool: str, arguments: dict, answers=None, asked: list | None = None, times: int = 1):
    """
    Calls a lineage tool `times` times through an in-process client. `answers` play the
    user filling the form; None is a client that can't show forms.
    """
    queue = list(answers or [])

    async def callback(_context, params):
        if asked is not None:
            asked.append(params.message)
        return queue.pop(0)

    async def run():
        mcp = MCPServer("structure")
        lineage_tools.register_lineage_tools(mcp)
        texts = []
        async with Client(mcp, elicitation_callback=callback if answers is not None else None) as client:
            for _ in range(times):
                result = await client.call_tool(tool, arguments)
                assert not result.is_error, result.content
                texts.append("\n".join(b.text for b in result.content if hasattr(b, "text")))
        return texts
    texts = asyncio.run(run())
    return texts if times > 1 else texts[0]


def _lineage(folder: str, **extra) -> dict:
    return {"sql_project_path": folder, **COLUMN, **extra}


# ---------------------------------------------------------------------------
# Does the project need a structure?
# ---------------------------------------------------------------------------

def test_a_project_with_no_configuration_and_other_names_needs_one(warehouse):
    assert structure.structure_state(warehouse) == "none"


def test_the_standard_names_need_nothing(write_files, tmp_path):
    root = write_files({
        "Wh.sqlproj": '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003"><ItemGroup>'
                      '<Build Include="bronze\\a.sql" /><Build Include="silver\\b.sql" /></ItemGroup></Project>',
        "bronze/a.sql": "CREATE TABLE bronze.a (Id INT)",
        "silver/b.sql": "CREATE VIEW silver.b AS SELECT a.Id FROM bronze.a a"}, str(tmp_path / "std"))
    assert structure.structure_state(root) == ""


def test_the_repositorys_file_is_never_proposed_over(warehouse):
    with open(os.path.join(warehouse, "lineage.yaml"), "w", encoding="utf-8") as handle:
        handle.write("version: 1\n")                    # the team's file, committed, saying nothing
    assert structure.structure_state(warehouse) == ""
    before = _listing(warehouse)
    text = _call("get_sql_project_lineage", _lineage(warehouse), answers=[])
    assert "the repository's" in text and "[!] Its layers place only 0 of the" in text
    assert _listing(warehouse) == before


def test_a_configuration_with_layers_is_left_alone(warehouse):
    own = _own_file(warehouse)
    os.makedirs(os.path.dirname(own), exist_ok=True)
    with open(own, "w", encoding="utf-8") as handle:
        handle.write("version: 1\nlayers: {bronze: stg, silver: etl, gold: [dwh, pbi]}\n")
    assert structure.structure_state(warehouse) == ""
    text = _call("get_sql_project_lineage", _lineage(warehouse), answers=[])
    assert "PROVISIONAL" not in text and "yours, outside the repository" in text


def test_a_configuration_that_places_almost_nothing_says_so(warehouse):
    own = _own_file(warehouse)
    os.makedirs(os.path.dirname(own), exist_ok=True)
    with open(own, "w", encoding="utf-8") as handle:
        handle.write("version: 1\nlayers: {bronze: raw, silver: clean, gold: mart}\ndefault_schema: dwh\n")
    assert structure.structure_state(warehouse) == ""   # it is the user's file: only a warning
    text = _call("get_sql_project_lineage", _lineage(warehouse), answers=[])
    assert "[!] Its layers place only 0 of the" in text


# ---------------------------------------------------------------------------
# The question
# ---------------------------------------------------------------------------

def test_the_answer_is_never_a_parameter_the_model_fills():
    async def schemas():
        mcp = MCPServer("structure")
        lineage_tools.register_lineage_tools(mcp)
        async with Client(mcp) as client:
            return {t.name: t.input_schema for t in (await client.list_tools()).tools}
    for name in ("get_sql_project_lineage", "get_end_to_end_lineage", "export_lineage"):
        assert "structure" not in asyncio.run(schemas())[name].get("properties", {})


def test_the_form_shows_the_structure_and_where_it_will_be_saved(warehouse):
    asked: list = []
    _call("get_sql_project_lineage", _lineage(warehouse), [_answer("not now")], asked)
    assert len(asked) == 1
    message = asked[0]
    assert "place 0 of its 8 objects" in message
    assert "bronze: stg" in message and "gold: pbi" in message
    assert "dwh" in message and "etl.vw_" in message          # the rule that closes the Parquet hand-off
    assert _own_file(warehouse) in message and "outside the repository" in message


def test_saying_save_writes_the_users_file_and_never_the_repository(warehouse):
    before = _listing(warehouse)
    asked: list = []
    first, second = _call("get_sql_project_lineage", _lineage(warehouse), [_answer("save")], asked, times=2)
    own = _own_file(warehouse)
    assert first.startswith("Saved the structure of 'Wh'") and own in first
    assert os.path.isfile(own)
    assert _listing(warehouse) == before, "nothing is written in the project or its repository"
    # The lineage goes through the Parquet, back to the files of the source system.
    assert "file:erp/Ledger/Ledger.parquet (Quantity)" in first
    assert "rule '" in first and "PROVISIONAL" not in first
    # Asked once, and the second call reads the saved file.
    assert len(asked) == 1 and "PROVISIONAL" not in second and "Saved the structure" not in second
    assert "file:erp/Ledger/Ledger.parquet (Quantity)" in second


def test_the_saved_file_is_the_structure_that_was_shown(warehouse):
    _call("get_sql_project_lineage", _lineage(warehouse), [_answer("save")])
    text = open(_own_file(warehouse), encoding="utf-8").read()
    assert "layers:" in text and "bronze: stg" in text and "gold: pbi" in text
    assert "rules:" in text and "from: 'etl.vw_" in text
    assert structure.structure_state(warehouse) == ""


def test_a_file_that_says_nothing_is_replaced_and_its_comments_kept(warehouse):
    own = _own_file(warehouse)
    os.makedirs(os.path.dirname(own), exist_ok=True)
    with open(own, "w", encoding="utf-8") as handle:
        handle.write("# my notes: ask the data team about the dwh schema\nversion: 1\n")
    assert structure.structure_state(warehouse) == "blank"
    text = _call("get_sql_project_lineage", _lineage(warehouse), [_answer("save")])
    assert text.startswith("Saved the structure")
    saved = open(own, encoding="utf-8").read()
    assert "bronze: stg" in saved and "# # my notes: ask the data team about the dwh schema" in saved


def test_saying_not_now_uses_it_for_the_session_without_saving(warehouse):
    asked: list = []
    first, second = _call("get_sql_project_lineage", _lineage(warehouse), [_answer("not now")], asked, times=2)
    assert not os.path.exists(_own_file(warehouse))
    for text in (first, second):
        assert "PROVISIONAL, not saved" in text and "file:erp/Ledger/Ledger.parquet (Quantity)" in text
    assert len(asked) == 1


def test_a_declined_form_is_provisional_too(warehouse):
    asked: list = []
    first, second = _call("get_sql_project_lineage", _lineage(warehouse),
                          [ElicitResult(action="decline")], asked, times=2)
    assert not os.path.exists(_own_file(warehouse))
    assert "PROVISIONAL" in first and "PROVISIONAL" in second and len(asked) == 1


def test_asking_for_changes_stops_and_sends_the_assistant_to_the_draft(warehouse):
    first, second = _call("get_sql_project_lineage", _lineage(warehouse),
                          [_answer("change", "dwh is gold, ignore pbi")], times=2)
    assert first.startswith("Nothing was run") and "dwh is gold, ignore pbi" in first
    assert "draft_lineage_config" in first and not os.path.exists(_own_file(warehouse))
    assert "PROVISIONAL" in second, "the question is not asked twice: the proposal is provisional"


def test_the_checking_tools_read_the_files_as_they_are(warehouse):
    _call("get_sql_project_lineage", _lineage(warehouse), [_answer("not now")])
    text = lineage_tools.test_lineage_config(warehouse)
    assert "none found: standard medallion" in text and "PROVISIONAL" not in text


def test_without_forms_the_assistant_is_asked_once(warehouse):
    first, second = _call("get_sql_project_lineage", _lineage(warehouse), times=2)
    assert first.startswith("Nothing was run yet") and "ask whether to keep it or change it" in first
    assert "```yaml" in first and "bronze: stg" in first and "save_lineage_config" in first
    assert not os.path.exists(_own_file(warehouse))
    assert "PROVISIONAL" in second and "file:erp/Ledger/Ledger.parquet (Quantity)" in second


def test_an_export_asks_the_same_question(warehouse):
    asked: list = []
    text = _call("export_lineage", {"sql_project_path": warehouse, "export_format": "csv"},
                 [_answer("save")], asked)
    assert len(asked) == 1 and text.startswith("Saved the structure")
    assert os.path.isfile(_own_file(warehouse))


def test_a_project_that_cannot_be_read_is_reported_by_the_tool_not_the_form(tmp_path):
    async def run():
        mcp = MCPServer("structure")
        lineage_tools.register_lineage_tools(mcp)
        async with Client(mcp) as client:
            return await client.call_tool("get_sql_project_lineage",
                                          {"sql_project_path": str(tmp_path / "nothing")})
    result = asyncio.run(run())
    assert result.is_error


def test_the_end_to_end_lineage_proposes_the_last_layer_from_the_model(write_files, tmp_path):
    from tests.test_end_to_end import FILES
    files = {k: v for k, v in FILES.items() if k != "db/lineage.yaml"}     # the shop, with no configuration
    root = write_files(files, str(tmp_path / "ws"))
    (tmp_path / "ws" / ".git").mkdir()
    db, pbip = os.path.join(root, "db"), os.path.join(root, "bi", "Shop.pbip")
    arguments = {"project_path": pbip, "sql_project_path": db, "target": "[Ratio]", "direction": "upstream"}
    asked: list = []
    text = _call("get_end_to_end_lineage", arguments, [_answer("save")], asked)
    assert len(asked) == 1 and "Power BI model 'Shop'" in asked[0]
    assert "gold: mart" in asked[0], "the schemas the model reads are the last layer"
    assert text.startswith("Saved the structure of ")
    assert "=== END-TO-END LINEAGE: Shop" in text and "PROVISIONAL" not in text
    assert "file:sales/orders/ (Price)" in text, "the chain reaches the files of the source system"


# ---------------------------------------------------------------------------
# Files a pipeline writes
# ---------------------------------------------------------------------------

LAYERS = "version: 1\nlayers: {bronze: stg, silver: etl, gold: [dwh, pbi]}\n"
RULE = ("rules:\n  - name: parquet-export\n    match: 'dwh\\.fact_(?P<name>\\w+)'\n"
        "    from: 'etl.vw_fact_{name}'\n")


def _configure(folder: str, text: str) -> str:
    path = os.path.join(folder, "lineage.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def test_a_view_over_files_written_by_a_similarly_named_object_is_flagged(warehouse):
    config = _configure(warehouse, LAYERS)
    text = lineage_tools.test_lineage_config(warehouse, config_path=config)
    assert "[!] Objects that only read files nothing in the project writes" in text
    assert "dwh.fact_sales <- file:common/fact/sales/fact_sales.parquet" in text
    assert "Nothing to fix" not in text
    rules = lineage_tools.suggest_lineage_rules(warehouse, config_path=config)
    assert "from: 'etl.vw_" in rules and "pbi.vw_" not in rules.split("rules:")[1].split("What each")[0]


def test_a_rule_closes_the_gap(warehouse):
    config = _configure(warehouse, LAYERS + RULE)
    assert "Nothing to fix" in lineage_tools.test_lineage_config(warehouse, config_path=config)
    text = lineage_tools.get_sql_project_lineage(warehouse, config_path=config, **COLUMN)
    assert "file:erp/Ledger/Ledger.parquet (Quantity)" in text and "rule 'parquet-export'" in text


def test_a_file_people_drop_is_not_a_missing_load(write_files, tmp_path):
    root = write_files({
        "Wh.sqlproj": '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003"><ItemGroup>'
                      '<Build Include="raw\\a.sql" /><Build Include="mart\\targets.sql" /></ItemGroup></Project>',
        "raw/a.sql": "CREATE TABLE raw.a (Id INT)",
        "mart/targets.sql": "CREATE VIEW mart.targets AS " + _openrowset("targets/*.csv", "Target int")},
        str(tmp_path / "csv"))
    config = _configure(root, "version: 1\nlayers: {bronze: raw, gold: mart}\n")
    lineage = sql_project.load_sql_lineage(root, config)
    assert list(lineage.checks["file_readers"]) == ["mart.targets"]
    assert suggest_rules(lineage)["handoffs"] == []
    assert "Nothing to fix" in lineage_tools.test_lineage_config(root, config_path=config)


def test_what_reads_the_target_is_never_proposed_as_its_source(warehouse):
    lineage = sql_project.load_sql_lineage(warehouse, _configure(warehouse, LAYERS))
    sources = {source for rule in suggest_rules(lineage)["rules"] for _t, source in rule.pairs}
    assert sources and all(s.startswith("etl.") for s in sources)


# ---------------------------------------------------------------------------
# The longest common part of two names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a, b, expected", [
    ("fact_stocks", "vw_fact_stocks", (0, 3, 11)),
    ("t01_orders", "vw_orders", (3, 2, 7)),
    ("abcxx", "xxabc", (0, 2, 3)),          # of two equally long parts, the first one in a
    ("abc", "xyz", (0, 0, 0)),
    ("", "abc", (0, 0, 0)),
])
def test_the_longest_common_part_of_two_names(a, b, expected):
    assert _common(a, b) == expected
