"""
Rule suggestions for lineage.yaml and exports of a lineage (Mermaid,
Markdown, CSV, JSON, saved files).
"""

from __future__ import annotations

import json
import os

import pytest

from mcp_data_platform_toolkit.lineage import lineage_tools
from mcp_data_platform_toolkit.lineage.suggestions import suggest_rules
from mcp_data_platform_toolkit.sql_project import config as lineage_config
from mcp_data_platform_toolkit.sql_project.project import load_sql_lineage
from tests.test_end_to_end import FILES as E2E_FILES
from tests.test_sql_lineage import CONFIG, PROJECT, SQLPROJ

SUGGEST = {
    "P.sqlproj": SQLPROJ,
    "raw.sql": "\nGO\n".join(f"CREATE TABLE raw.{name} (id INT, x INT)" for name in (
        "orders", "customers", "erp_invoices", "erp_payments", "crm_contacts", "crm_leads")) + "\n",
    "core.sql": "CREATE VIEW core.v_orders AS SELECT id, x FROM raw.orders\nGO\n"
                "CREATE VIEW core.v_items AS SELECT id, x FROM raw.orders\nGO\n"
                "CREATE TABLE core.s01_orders (id INT, x INT)\nGO\nCREATE TABLE core.s02_items (id INT, x INT)\nGO\n"
                "CREATE VIEW core.loaded AS SELECT id FROM raw.customers\n",
    "clean.sql": "CREATE TABLE clean.orders (id INT, x INT)\nGO\nCREATE TABLE clean.customers (id INT, x INT)\n",
    "curated.sql": "CREATE TABLE curated.orders (id INT, x INT)\nGO\nCREATE TABLE curated.items (id INT, x INT)\n",
    "snap.sql": "CREATE TABLE snap.s01_orders (id INT, x INT)\nGO\nCREATE TABLE snap.s02_items (id INT, x INT)\nGO\n"
                "CREATE TABLE snap.unrelated_thing (id INT)\n",
    "lineage.yaml": "layers:\n  bronze: raw\n  silver: [core, clean, curated]\n  gold: snap\n",
}


@pytest.fixture
def suggest_project(write_files, tmp_path):
    return write_files(SUGGEST, str(tmp_path / "s"))


def _by_target(result) -> dict:
    return {t: (r.match, r.source) for r in result["rules"] for t, _s in r.pairs}


def test_rules_are_inferred_from_the_names(suggest_project):
    result = suggest_rules(load_sql_lineage(suggest_project))
    rules = _by_target(result)
    assert rules["clean.orders"] == (r"clean\.(?P<name>\w+)", "raw.{name}")
    assert rules["curated.items"] == (r"curated\.(?P<name>\w+)", "core.v_{name}")
    assert rules["snap.s02_items"] == (r"snap\.(?P<name>s\d{2}_\w*)", "core.{name}")
    assert result["unmatched"] == ["snap.unrelated_thing"]


def test_each_proposal_is_tried_on_the_project(suggest_project):
    result = suggest_rules(load_sql_lineage(suggest_project))
    snapshot = next(r for r in result["rules"] if r.match.startswith("snap"))
    assert sorted(snapshot.matched) == ["snap.s01_orders", "snap.s02_items"] and snapshot.found == 2
    systems = next(r for r in result["rules"] if r.external)
    assert (systems.match, systems.source) == (r"raw\.(?P<system>[a-z0-9]+)_(?P<table>\w+)", "{system}.{table}")
    assert "crm (2)" in systems.note and "erp (2)" in systems.note


def test_the_suggested_yaml_is_valid_and_does_what_it_says(suggest_project):
    text = lineage_tools.suggest_lineage_rules(suggest_project)
    assert "rules:\n  - name:" in text
    snippet = text.split("To paste under 'rules:' in lineage.yaml (check each one first):\n\n", 1)[1]
    snippet = snippet.split("\n\nWhat each rule", 1)[0]
    with open(os.path.join(suggest_project, "lineage.yaml"), "a", encoding="utf-8") as handle:
        handle.write(snippet + "\n")
    lineage_config.load_config(os.path.join(suggest_project, "lineage.yaml"))      # valid
    checks = load_sql_lineage(suggest_project).checks
    assert checks["loads_not_found"] == ["snap.unrelated_thing"]


def test_a_too_broad_rule_is_flagged(suggest_project):
    result = suggest_rules(load_sql_lineage(suggest_project))
    copies = next(r for r in result["rules"] if r.match.startswith(r"curated"))
    assert copies.already_loaded == []
    clean = next(r for r in result["rules"] if r.match.startswith(r"clean"))
    assert clean.found == len(clean.matched) == 2


def test_numbered_names_become_one_rule(write_files, tmp_path):
    files = {"P.sqlproj": SQLPROJ,
             "a.sql": "CREATE TABLE stg.erp_customers (id INT)\nGO\nCREATE TABLE stg.erp_orders (id INT)\nGO\n"
                      "CREATE VIEW etl.vw_customers AS SELECT id FROM stg.erp_customers\nGO\n"
                      "CREATE VIEW etl.vw_orders AS SELECT id FROM stg.erp_orders\nGO\n"
                      "CREATE TABLE tmp.t01_customers (id INT)\nGO\nCREATE TABLE tmp.t02_orders (id INT)\n",
             "lineage.yaml": "layers:\n  bronze: stg\n  silver: [etl, tmp]\n"}
    root = write_files(files, str(tmp_path / "numbered"))
    rule = next(r for r in suggest_rules(load_sql_lineage(root))["rules"] if r.match.startswith("tmp"))
    assert (rule.match, rule.source) == (r"tmp\.t\d{2}(?P<name>\w+)", "etl.vw{name}")
    assert sorted(rule.matched) == ["tmp.t01_customers", "tmp.t02_orders"] and rule.found == 2


def test_nothing_to_suggest(write_files, tmp_path):
    root = write_files({**PROJECT, "lineage.yaml": CONFIG}, str(tmp_path / "n"))
    assert "nothing to suggest" in lineage_tools.suggest_lineage_rules(root)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@pytest.fixture
def shop(write_files, tmp_path):
    root = write_files(E2E_FILES, str(tmp_path / "ws"))
    return os.path.join(root, "bi", "Shop.pbip"), os.path.join(root, "db")


def test_mermaid_export_draws_layers_and_rules(shop):
    _model, db = shop
    text = lineage_tools.export_lineage("mermaid", sql_project_path=db)
    assert text.startswith("flowchart LR\n")
    assert 'subgraph L1["bronze"]' in text and 'subgraph L3["gold"]' in text
    assert "-.->|rule history|" in text and "-->|merge|" in text


def test_markdown_csv_and_json_exports(shop):
    model, db = shop
    markdown = lineage_tools.export_lineage("markdown", project_path=model, sql_project_path=db,
                                            target="Shop/Overview/Ratio", direction="upstream")
    assert markdown.startswith("# End-to-end lineage of Shop\n")
    assert "```mermaid\nflowchart LR" in markdown and "| Source | Target | How | Evidence |" in markdown
    csv_text = lineage_tools.export_lineage("csv", project_path=model, sql_project_path=db, level="column",
                                            target="Targets[Stretch]", direction="upstream")
    assert csv_text.splitlines()[0] == "source,source_layer,target,target_layer,kind,confidence,rule,evidence"
    assert "mart.targets.Target,gold,Targets[Stretch],semantic model,native query" in csv_text
    payload = json.loads(lineage_tools.export_lineage("json", sql_project_path=db, target="mart.sales"))
    assert payload["title"].startswith("Lineage of the database project") and payload["edges"]


def test_an_export_too_large_is_not_cut(shop, monkeypatch):
    model, db = shop
    monkeypatch.setattr(lineage_tools, "MAX_TOOL_OUTPUT_CHARS", 200)
    text = lineage_tools.export_lineage("mermaid", project_path=model, sql_project_path=db)
    assert "more than a response holds" in text and "save_lineage_export" in text


def test_saving_an_export(shop, tmp_path, monkeypatch):
    model, db = shop
    path = os.path.join(os.path.dirname(model), "lineage.md")
    message = lineage_tools.save_lineage_export(path, project_path=model, sql_project_path=db)
    assert message.startswith("Saved the markdown export")
    with open(path, encoding="utf-8") as handle:
        assert handle.read().startswith("# End-to-end lineage of Shop")
    with pytest.raises(FileExistsError):
        lineage_tools.save_lineage_export(path, project_path=model)
    assert lineage_tools.save_lineage_export(path, "mermaid", project_path=model, overwrite=True).startswith("Saved")
    with pytest.raises(FileNotFoundError):
        lineage_tools.save_lineage_export(os.path.join(os.path.dirname(model), "missing", "x.md"),
                                          project_path=model)
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path / "elsewhere"))
    with pytest.raises(ValueError, match="outside the folders"):
        lineage_tools.save_lineage_export(str(tmp_path / "y.md"), project_path=model)


def test_an_export_is_saved_only_in_the_project_folders(shop, tmp_path, monkeypatch):
    model, db = shop
    monkeypatch.delenv("MCP_ALLOWED_ROOTS", raising=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="outside the project folders"):
        lineage_tools.save_lineage_export(str(outside / "lineage.md"), project_path=model)
    # The database project's folder is allowed only when the export reads it.
    with pytest.raises(ValueError, match="outside the project folders"):
        lineage_tools.save_lineage_export(os.path.join(db, "lineage.md"), project_path=model)
    assert lineage_tools.save_lineage_export(os.path.join(db, "lineage.json"), "json",
                                             sql_project_path=db).startswith("Saved the json export")
    # With MCP_ALLOWED_ROOTS the allowed roots decide, not the project folders.
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    assert lineage_tools.save_lineage_export(str(outside / "lineage.md"),
                                             project_path=model).startswith("Saved")
    assert sorted(os.listdir(outside)) == ["lineage.md"]


def test_an_export_keeps_the_extension_of_its_format(shop):
    model, _db = shop
    folder = os.path.dirname(model)
    for name, export_format in (("run.bat", "markdown"), ("lineage", "markdown"),
                                ("lineage.md", "csv"), ("lineage.csv", "json"), ("lineage.ps1", "mermaid")):
        with pytest.raises(ValueError, match="has another extension"):
            lineage_tools.save_lineage_export(os.path.join(folder, name), export_format, project_path=model)
    assert not {"run.bat", "lineage", "lineage.ps1"} & set(os.listdir(folder))
    assert lineage_tools.save_lineage_export(os.path.join(folder, "lineage.MMD"), "mermaid",
                                             project_path=model).startswith("Saved the mermaid export")


def test_save_is_a_full_access_tool():
    import asyncio
    import sys

    original = list(sys.argv)
    try:
        from mcp_data_platform_toolkit import server
        sys.argv = ["server.py", "--access=read_only", "--msft-bridge=off"]
        read_only = {t.name for t in asyncio.run(server.build_server().list_tools())}
        sys.argv = ["server.py", "--access=full_access", "--msft-bridge=off"]
        full = {t.name for t in asyncio.run(server.build_server().list_tools())}
    finally:
        sys.argv = original
    assert {"export_lineage", "suggest_lineage_rules", "get_end_to_end_lineage",
            "draft_lineage_config"} <= read_only
    for writer in ("save_lineage_export", "save_lineage_config"):
        assert writer not in read_only and writer in full


def test_the_readme_example_is_the_real_export(shop):
    """
    The lineage example of the README and of docs/lineage.md is this export: a change of the output must update
    both. docs/lineage.md draws it top to bottom (`flowchart TB`) so it fits GitHub's column; the README keeps it
    left to right but drops the edge labels and breaks long names on two lines, so it stays readable when shrunk.
    """
    import re

    model, db = shop
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    export = lineage_tools.export_lineage("mermaid", project_path=model, sql_project_path=db, level="column",
                                          target="Shop/Overview/Ratio", direction="upstream")
    assert export.startswith("flowchart LR\n")
    body = export.strip().partition("\n")[2]

    def shown(*page):
        with open(os.path.join(root, *page), encoding="utf-8") as handle:
            return re.search(r"```mermaid\n(.*?)```", handle.read(), re.S).group(1)

    def squeezed(text):
        return re.sub(r"\s|<br/>", "", text)

    lineage_page = shown("docs", "lineage.md")
    assert lineage_page.startswith("flowchart TB\n") and lineage_page.strip().partition("\n")[2] == body
    readme = shown("README.md")
    assert readme.startswith("flowchart LR\n")
    assert squeezed(readme.partition("\n")[2]) == squeezed(re.sub(r"-->\|[^|]*\| ", "--> ", body))
