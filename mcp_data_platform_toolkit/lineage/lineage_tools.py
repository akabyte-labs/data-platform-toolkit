"""
MCP tools of the lineage: the database project and end-to-end lineage, its
configuration ('lineage.yaml': drafted with questions, checked, documented)
and its exports.

The tools that write files (save_lineage_export, save_lineage_config) are
registered in full access mode only; all the others only read, with one
exception: when a database project has no structure, the tools that read its
lineage show the one inferred from its SQL in a form of the client and, when
the user answers "save", write it as the user's own file in the user's folder
(see `structure`).
"""

from __future__ import annotations

from pathlib import Path

from mcp_data_platform_toolkit.lineage import config_review, draft_notice
from mcp_data_platform_toolkit.lineage import graph as lineage_graph
from mcp_data_platform_toolkit.lineage.config_review import LineageReview
from mcp_data_platform_toolkit.mcp_common.errors import ToolError, register_tools
from mcp_data_platform_toolkit.mcp_common.folder_choice import FirstChoice
from mcp_data_platform_toolkit.mcp_common.output_shaping import MAX_TOOL_OUTPUT_CHARS, shape_lines, shape_payload
from mcp_data_platform_toolkit.lineage.structure import ModelStructure, SqlStructure, settle_structure

# Shipped inside the package, next to this module.
GUIDE_FILE = Path(__file__).resolve().parent / "lineage_config_guide.md"

DIRECTIONS = ("upstream", "downstream", "both")
LEVELS = ("object", "column")
FORMATS = ("edges", "tree", "json")

# Problems of the analysis, in the order they are shown, with their title.
PROBLEM_TITLES = (
    ("parse_errors", "Not readable by the parser"),
    ("read_without_parser", "Tables read from their column list only (the parser can't read them)"),
    ("dynamic_sql", "Dynamic SQL (not analyzable)"),
    ("not_analyzed", "Not analyzed"),
    ("ambiguous", "Names without schema matching several objects"),
    ("unknown_columns", "Columns not determined"),
    ("column_errors", "Column lineage not computed"),
    ("duplicates", "Objects defined more than once (the first one is used)"),
    ("skipped_files", "Files skipped"),
    ("excluded_files", "Files excluded by the .sqlproj (deployment scripts, not built)"),
)


def _load(sql_project_path: str, config_path: str = "", use_proposed: bool = True):
    """
    The lineage of the project, or a clear error when a dependency is missing.
    `use_proposed=False` reads the configuration files as they are, without
    the structure proposed in this session: the tools that check them.
    """
    try:
        from mcp_data_platform_toolkit.sql_project.project import load_sql_lineage
    except ImportError as exc:
        raise ToolError(
            f"The lineage needs the '{exc.name}' package, which is not installed in the "
            f"Python environment of the server. Reinstall it: pip install --upgrade mcp-data-platform-toolkit") from exc
    return load_sql_lineage(sql_project_path, config_path, use_proposed)


def _noticed(notice: str, text: str) -> str:
    """What the user must know before the lineage (the structure just saved), then the lineage."""
    return f"{notice}\n\n{text}" if notice else text


def _choice(value: str, allowed: tuple, name: str, default: str) -> str:
    text = (value or default).strip().lower()
    if text not in allowed:
        raise ValueError(f"'{name}' must be one of: {', '.join(allowed)} (got '{value}').")
    return text


def _normalize(name: str) -> str:
    parts = []
    for part in str(name or "").strip().split("."):
        text = part.strip()
        if len(text) >= 2 and text[0] in "[\"" and text[-1] in "]\"":
            text = text[1:-1]
        parts.append(text.lower())
    return ".".join(parts)


def _find_object(graph, text: str) -> list[str]:
    """Node ids of the objects named `text`: 'schema.object', 'object' or 'file:path'."""
    target = _normalize(text) if not text.strip().lower().startswith("file:") else text.strip().lower()
    exact = [k for k, n in graph.nodes.items() if n.label.lower() == target or k == target]
    if exact:
        return exact
    return [k for k, n in graph.nodes.items()
            if "." in n.label and n.label.lower().rsplit(".", 1)[1] == target]


def _resolve_target(lineage, target: str, level: str) -> tuple[str, list[str]]:
    """(level, root node ids) of the requested target."""
    objects = _find_object(lineage.objects, target)
    if len(objects) == 1:
        if level == "column":
            prefix = objects[0] + "|"
            return "column", [k for k in lineage.columns.nodes if k.startswith(prefix)]
        return "object", objects
    if len(objects) > 1:
        names = ", ".join(sorted(lineage.objects.nodes[k].label for k in objects))
        raise ValueError(f"'{target}' matches several objects: {names}. Give the schema too.")

    owner, _dot, column = target.strip().rpartition(".")
    if owner and column:
        candidates = _find_object(lineage.objects, owner)
        wanted = _normalize(column)
        found = [f"{k}|{wanted}" for k in candidates if f"{k}|{wanted}" in lineage.columns.nodes]
        if len(found) == 1:
            return "column", found
        if len(found) > 1:
            names = ", ".join(sorted(lineage.columns.nodes[k].label for k in found))
            raise ValueError(f"'{target}' matches several columns: {names}. Give the schema too.")
        if len(candidates) == 1:
            obj = lineage.objects.nodes[candidates[0]]
            raise LookupError(f"Column '{column}' not found in {obj.label}: its columns are "
                              f"{_columns_of(lineage, candidates[0]) or 'not known'}.")
    raise LookupError(f"'{target}' is not an object (or 'schema.object.column') of the database "
                      f"project '{lineage.project.name}'.")


def _columns_of(lineage, object_key: str) -> str:
    obj = lineage.analysis.objects.get(object_key)
    return ", ".join(obj.columns) if obj is not None and obj.columns else ""


def _configuration_name(lineage) -> str:
    config = lineage.config
    if config.provisional:
        return "provisional structure, not saved"
    return config.path or "standard medallion"


def _config_line(lineage) -> str:
    """Which configuration file is in use, whose it is, the one it hides, and whether it is a draft."""
    line = _config_file_line(lineage)
    return f"{line}\n{draft_notice.DRAFT_IN_USE}" if lineage.config.draft else line


def _config_file_line(lineage) -> str:
    import os

    from mcp_data_platform_toolkit.sql_project.config import find_config_file, personal_config_file
    from mcp_data_platform_toolkit.sql_project.project import layer_coverage
    project = lineage.project
    personal = personal_config_file(project.folder, project.sqlproj)
    path = lineage.config.path
    layers = ", ".join(f"{name} [{', '.join(schemas)}]" for name, schemas in lineage.config.layers)
    if lineage.config.provisional:
        return (f"Configuration: [!] PROVISIONAL, not saved: the structure inferred from the SQL, layers "
                f"{layers}. Confirm it to keep it as yours in '{personal}' (outside the repository): "
                f"save_lineage_config in full access mode, or paste it in the file that "
                f"'mcp-data-platform-toolkit --lineage-config' opens.")
    placed, total = layer_coverage(lineage)
    thin = (f". [!] Its layers place only {placed} of the {total} objects of the project: check the schema "
            f"names (test_lineage_config) or let draft_lineage_config propose the structure"
            if total and placed < 0.5 * total else "")
    if path and os.path.normcase(os.path.abspath(path)) == os.path.normcase(personal):
        return f"Configuration: {path} (yours, outside the repository){thin}"
    if path:
        hidden = (f"; yours, '{personal}', is not used: the repository's wins"
                  if os.path.isfile(personal) and path == find_config_file(project.folder) else "")
        return f"Configuration: {path} (the repository's{hidden}){thin}"
    names = ", ".join(lineage.config.layer_names)
    return (f"Configuration: none found: standard medallion, layers {names} = schemas with the "
            f"same name. save_lineage_config saves one in '{personal}' (yours, outside the "
            f"repository){thin}")


def _problem_summary(lineage) -> str:
    problems = lineage.analysis.problems
    parts = [f"{len(problems[key])} {title.split(' (')[0].lower()}"
             for key, title in PROBLEM_TITLES if problems.get(key) and key != "excluded_files"]
    if not parts:
        return ""
    return "Warnings: " + "; ".join(parts) + ". Details: test_lineage_config."


def get_sql_project_lineage(
    sql_project_path: str,
    target: str = "",
    direction: str = "both",
    level: str = "object",
    depth: int = 0,
    output_format: str = "edges",
    config_path: str = "",
    structure: SqlStructure = None,
) -> str:
    """
    Data lineage of a SQL Server database project (.sqlproj, T-SQL): which
    objects and which columns each table and view comes from, through views,
    INSERT, MERGE, UPDATE, SELECT INTO and CREATE TABLE AS, also inside
    stored procedures (temporary tables are followed through). Objects are
    labelled with their medallion layer, as configured in the project's
    'lineage.yaml' (see get_lineage_config_guide); loads the SQL doesn't
    show come from the rules of that file and are marked "rule '<name>'".

    A project with no configuration, whose schemas the standard layers
    (bronze, silver, gold) don't name, would show almost nothing: the
    structure inferred from its SQL is then shown to the user, who keeps it
    (it is saved in their folder, outside the repository), changes it or
    uses it for the session only. Until it is saved, every response says the
    structure is provisional.

    Args:
        sql_project_path: The .sqlproj file or the folder of the database project.
        target: Optional object ('schema.object') or column ('schema.object.column')
            to start from. Without it, the whole project.
        direction: 'upstream' (where it comes from), 'downstream' (what uses it)
            or 'both'.
        level: 'object' or 'column'. With an object target, 'column' follows each of
            its columns. A column target always uses 'column'.
        depth: Steps to follow from the target (0 = all).
        output_format: 'edges' (one line per edge, the most compact), 'tree'
            (indented from the target) or 'json'.
        config_path: Optional configuration file, to use instead of the one
            found (the repository's 'lineage.yaml', else the user's own).
    """
    direction = _choice(direction, DIRECTIONS, "direction", "both")
    level = _choice(level, LEVELS, "level", "object")
    output_format = _choice(output_format, FORMATS, "output_format", "edges")
    try:
        depth = max(int(depth or 0), 0)
    except (TypeError, ValueError):
        raise ValueError(f"'depth' must be a whole number (got '{depth}').") from None
    stop, notice = settle_structure(sql_project_path, "", config_path, structure)
    if stop:
        return stop
    lineage = _load(sql_project_path, config_path)

    roots: list[str] = []
    if target and target.strip():
        level, roots = _resolve_target(lineage, target, level)
    graph = lineage.columns if level == "column" else lineage.objects
    selected = graph.select(roots, direction, depth) if roots else graph

    title = f"=== SQL LINEAGE: {lineage.project.name} ({level} level) ==="
    header = [title, _config_line(lineage)]
    if roots:
        header.append(f"Target: {target.strip()}, direction {direction}"
                      + (f", depth {depth}" if depth else ""))
    edges_by_rule = sum(1 for e in selected.edges if e.rule)
    header.append(f"{len(selected.nodes)} nodes, {len(selected.edges)} edges"
                  + (f" ({edges_by_rule} from rules)" if edges_by_rule else "")
                  + f". Layers: {lineage_graph.layer_counts(selected) or '-'}")
    summary = _problem_summary(lineage)
    if summary:
        header.append(summary)
    payload = {"project": lineage.project.name, "level": level,
               "configuration": _configuration_name(lineage),
               "warnings": {k: v for k, v in lineage.analysis.problems.items()
                            if k != "excluded_files"}}
    return _noticed(notice, _render(selected, roots, direction, level, header, output_format, payload))


def _render(selected, roots, direction, level, header, output_format, payload) -> str:
    """The selected graph in the requested format, within the output budget."""
    narrow = ("Narrow it with 'target' (and 'direction', 'depth') or use level 'object'."
              if not roots or level == "column" else "Narrow it with 'depth' or 'direction'.")
    if output_format == "json":
        return shape_payload({**payload, **lineage_graph.to_payload(selected)}, MAX_TOOL_OUTPUT_CHARS)
    if output_format == "tree":
        tree_roots = roots or sorted(k for k in selected.nodes if not selected.incoming().get(k))
        tree_direction = direction if roots else "downstream"
        return lineage_graph.format_tree(selected, tree_roots, tree_direction, header,
                                         MAX_TOOL_OUTPUT_CHARS, narrow)
    return lineage_graph.format_edges(selected, header, MAX_TOOL_OUTPUT_CHARS, narrow)


# ---------------------------------------------------------------------------
# End-to-end lineage
# ---------------------------------------------------------------------------

def _load_end_to_end(project_path, sql_project_path, reports_path, config_path):
    try:
        from mcp_data_platform_toolkit.lineage.end_to_end import build_end_to_end
    except ImportError as exc:
        raise ToolError(
            f"The lineage needs the '{exc.name}' package, which is not installed in the "
            f"Python environment of the server. Reinstall it: pip install --upgrade mcp-data-platform-toolkit") from exc
    return build_end_to_end(project_path, sql_project_path, reports_path, config_path)


def _coverage_line(e2e) -> str:
    traced, columns = e2e.checks["data_columns"]
    reaching, visuals = e2e.checks["visuals"]
    line = (f"Coverage: {traced} of {columns} model data columns traced to their source; "
            f"{reaching} of {visuals} visuals reach a column outside the model.")
    if e2e.reports.missing:
        line += (f" {len(e2e.reports.missing)} report(s) NOT analyzed: their visuals are not in these "
                 f"numbers.")
    return line


def _not_analyzed_lines(e2e) -> list[str]:
    """The reports left out of the lineage, one flagged line each kind: they must not be missed."""
    lines = []
    if e2e.reports.missing:
        lines.append(f"[!] {len(e2e.reports.missing)} report(s) NOT analyzed, so what they use is missing "
                     f"from this lineage (PBIR-Legacy reports: upgrade them to PBIR): "
                     + "; ".join(e2e.reports.missing))
    elsewhere = [entry for entry in e2e.reports.not_analyzed if entry not in e2e.reports.missing]
    if elsewhere:
        lines.append("Reports of another model, not part of this one: " + "; ".join(elsewhere))
    return lines


def get_end_to_end_lineage(
    project_path: str,
    target: str = "",
    direction: str = "both",
    level: str = "column",
    sql_project_path: str = "",
    reports_path: str = "",
    depth: int = 0,
    output_format: str = "edges",
    config_path: str = "",
    structure: ModelStructure = None,
) -> str:
    """
    End-to-end lineage, from the source column to the report visual and back:
    database project (medallion layers, as in get_sql_project_lineage) ->
    Power Query of each table (renames, computed columns, merges, appends,
    native queries, Direct Lake) -> model columns, calculated columns,
    measures, calculation groups, field parameters -> visuals, filters,
    drillthrough fields, bookmarks and report-level measures of the PBIR
    reports bound to the model. 'upstream' from a visual gives every source
    column behind it; 'downstream' from a source column is the impact
    analysis: every measure and visual that changes with it.

    With a database project that has no configuration and whose schemas the
    standard layers (bronze, silver, gold) don't name, the structure inferred
    from its SQL is first shown to the user, who keeps it (saved in their
    folder, outside the repository), changes it or uses it for the session
    only; until it is saved, every response says it is provisional. Always
    report the Coverage line and the reports "not analyzed" as they are.

    Args:
        project_path: The .pbip file, the project folder or the .SemanticModel folder.
        target: Optional starting point: '[Measure]', 'Table[Column]', 'Table',
            'schema.object' or 'schema.object.column' of the database,
            'Report', 'Report/Page' or 'Report/Page/Visual' (visual title, type or id).
            Without it, the whole lineage.
        direction: 'upstream', 'downstream' or 'both'.
        level: 'column' (the lineage itself) or 'object' (tables, views, model
            tables, measures, report pages).
        sql_project_path: Optional database project (.sqlproj or its folder) that
            feeds the model: with it, database columns are followed to their sources.
        reports_path: Optional folder with the reports; by default the reports next
            to the model.
        depth: Steps to follow from the target (0 = all).
        output_format: 'edges', 'tree' or 'json'.
        config_path: Optional configuration file of the database project, to
            use instead of the one found (the repository's, else the user's own).
    """
    direction = _choice(direction, DIRECTIONS, "direction", "both")
    level = _choice(level, LEVELS, "level", "column")
    output_format = _choice(output_format, FORMATS, "output_format", "edges")
    try:
        depth = max(int(depth or 0), 0)
    except (TypeError, ValueError):
        raise ValueError(f"'depth' must be a whole number (got '{depth}').") from None
    stop, notice = settle_structure(sql_project_path, project_path, config_path, structure)
    if stop:
        return stop
    e2e = _load_end_to_end(project_path, sql_project_path, reports_path, config_path)

    from mcp_data_platform_toolkit.lineage.end_to_end import resolve_target
    roots = resolve_target(e2e, target, level) if target and target.strip() else []
    graph = e2e.columns if level == "column" else e2e.objects
    selected = graph.select(roots, direction, depth) if roots else graph

    header = [f"=== END-TO-END LINEAGE: {e2e.name} ({level} level) ==="]
    if e2e.sql is not None:
        header.append(f"Database project: {e2e.sql.project.name}. {_config_line(e2e.sql)}")
    else:
        header.append("Database project: not given (the database columns the model reads have "
                      "no layer; pass 'sql_project_path' to follow them to their sources)")
    header.append("Reports analyzed: " + (", ".join(e2e.reports.analyzed) or "none found"))
    header.extend(_not_analyzed_lines(e2e))
    if roots:
        header.append(f"Target: {target.strip()}, direction {direction}"
                      + (f", depth {depth}" if depth else ""))
    header.append(f"{len(selected.nodes)} nodes, {len(selected.edges)} edges. "
                  f"Layers: {lineage_graph.layer_counts(selected) or '-'}")
    header.append(_coverage_line(e2e))
    warnings = _end_to_end_warnings(e2e)
    if warnings:
        header.append("Warnings: " + "; ".join(warnings) + ". Details: test_lineage_config "
                      "with 'project_path'.")
    payload = {"model": e2e.name, "level": level, "checks": {
        k: v for k, v in e2e.checks.items() if k not in ("visuals_without_source",)}}
    return _noticed(notice, _render(selected, roots, direction, level, header, output_format, payload))


def _end_to_end_warnings(e2e) -> list[str]:
    checks, problems = e2e.checks, e2e.model.problems
    warnings = []
    if checks["reads_other_layers"]:
        warnings.append(f"{len(checks['reads_other_layers'])} model sources outside the "
                        f"'{checks['last_layer']}' layer")
    if checks["not_in_project"]:
        warnings.append(f"{len(checks['not_in_project'])} model sources not in the database project")
    if problems.get("not_traced"):
        warnings.append(f"{len(problems['not_traced'])} model columns not traced")
    if problems.get("native_queries"):
        warnings.append(f"{len(problems['native_queries'])} native query problems")
    if e2e.sql is not None:
        summary = _problem_summary(e2e.sql)
        if summary:
            warnings.append("database project: " + summary[len("Warnings: "):].split(". Details")[0])
    return warnings


# ---------------------------------------------------------------------------
# Configuration check
# ---------------------------------------------------------------------------

_LIST_LIMIT = 15


def _listed(items: list[str], limit: int = _LIST_LIMIT) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" ... and {len(items) - limit} more" if len(items) > limit else "")


def test_lineage_config(sql_project_path: str, config_path: str = "", project_path: str = "",
                        reports_path: str = "") -> str:
    """
    Checks the lineage configuration ('lineage.yaml') of a database project
    against the project itself, and says what to fix: problems of the file
    (with their line), the objects of each layer, schemas in no layer, what
    each rule matches and the sources it doesn't find, tables nothing loads,
    flows going backwards between layers, columns that reach no source, and
    the statements that couldn't be analyzed. Use it after every change to
    the file, until only the expected items are left.

    With 'project_path' it also checks the Power BI side: model tables that
    read from a layer other than the last one or from objects not in the
    project, model columns whose source can't be traced (and the Power Query
    step that stops them), native queries that can't be read, visuals that
    reach no column outside the model.

    Args:
        sql_project_path: The .sqlproj file or the folder of the database project.
        config_path: Optional configuration file, to use instead of the one
            found (the repository's 'lineage.yaml', else the user's own).
        project_path: Optional Power BI project (.pbip, its folder or the
            .SemanticModel folder) fed by the database project.
        reports_path: Optional folder with the reports; by default the reports next
            to the model.
    """
    from mcp_data_platform_toolkit.sql_project.config import ConfigError
    try:
        lineage = _load(sql_project_path, config_path, use_proposed=False)
    except ConfigError as exc:
        lines = ["=== LINEAGE CONFIGURATION CHECK ===", f"File: {exc.path}",
                 f"[!] The file has {len(exc.problems)} problem(s): nothing else can be "
                 f"checked until they are fixed."]
        lines.extend(f"  - {p}" for p in exc.problems)
        lines.append("The rules for writing it: get_lineage_config_guide.")
        return "\n".join(lines)

    checks = lineage.checks
    analysis = lineage.analysis
    lines = ["=== LINEAGE CONFIGURATION CHECK ===",
             f"Database project: {lineage.project.sqlproj}",
             _config_line(lineage),
             f"Read {analysis.files} .sql files, {analysis.statements} statements with lineage, "
             f"{len(lineage.objects.edges)} object edges, {len(lineage.columns.edges)} column edges.",
             "", "Layers (flow order):"]
    for name, schemas in lineage.config.layers:
        objects = checks["by_layer"].get(name, [])
        lines.append(f"  {name} [{', '.join(schemas)}]: {len(objects)} objects"
                     + ("  [!] no object: check the schema names" if not objects else ""))
    if checks["ignored"]:
        lines.append("  ignored: " + ", ".join(f"{s} ({n})" for s, n in sorted(checks["ignored"].items())))

    todo = 0
    if checks["unassigned"]:
        todo += 1
        lines += ["", "[!] Schemas in no layer: add them to a layer or to 'ignore'"]
        lines += [f"  {schema}: {_listed(sorted(names))}"
                  for schema, names in sorted(checks["unassigned"].items())]

    if lineage.config.rules:
        lines += ["", "Rules:"]
        for rule in lineage.config.rules:
            entry = analysis.rules.get(rule.name, {})
            matched = entry.get("matched", [])
            kind = " (external source)" if rule.external else ""
            if not matched:
                todo += 1
                lines.append(f"  [!] {rule.name}{kind}: matches no object. 'match' must match the "
                             f"whole 'schema.object' name")
                continue
            lines.append(f"  {rule.name}{kind}: {len(matched)} objects, {entry.get('found', 0)} "
                         f"with their source: {_listed(matched, 8)}")
            if entry.get("missing"):
                todo += 1
                lines.append(f"    [!] source not in the project: {_listed(entry['missing'], 8)}")

    if checks["loads_not_found"]:
        todo += 1
        lines += ["", "[!] Tables nothing loads in the SQL: if a pipeline or a generic procedure "
                      "loads them, describe it with a rule"]
        lines.append("  " + _listed(checks["loads_not_found"], 30))
    handoffs = _file_handoffs(lineage)
    if handoffs:
        todo += 1
        lines += ["", "[!] Objects that only read files nothing in the project writes, and are named like "
                      "objects that could have written them (a pipeline copies the data there, so the "
                      "lineage stops at the file): describe what writes them with a rule; "
                      "suggest_lineage_rules proposes it from the names"]
        lines.append("  " + _listed([f"{label} <- {', '.join(files[:2])}{' ...' if len(files) > 2 else ''}"
                                     for label, files in handoffs.items()], 10))
    if checks["backward"]:
        todo += 1
        lines += ["", "[!] Flows going backwards (from a later layer to an earlier one): "
                      "check the layer of these schemas"]
        lines += [f"  {line}" for line in checks["backward"][:_LIST_LIMIT]]
    if checks["lookups"]:
        lines += ["", f"Lookups ({len(checks['lookups'])}): a later layer giving keys to an earlier one, "
                      f"fewer columns than it receives from it. Expected in staging; nothing to fix"]
        lines += [f"  {line}" for line in checks["lookups"][:_LIST_LIMIT]]
        if len(checks["lookups"]) > _LIST_LIMIT:
            lines.append(f"  ... and {len(checks['lookups']) - _LIST_LIMIT} more")
    if checks["columns_without_source"]:
        lines += ["", "Columns that reach no source (constants, columns of tables nothing loads, "
                      "or ETL columns to list in 'technical_columns'):"]
        lines += [f"  {layer}: {len(cols)}: {_listed(cols, 10)}"
                  for layer, cols in checks["columns_without_source"].items() if cols]

    problems = analysis.problems
    shown = [(key, title) for key, title in PROBLEM_TITLES if problems.get(key)]
    if shown:
        lines += ["", "Statements and files:"]
        for key, title in shown:
            lines.append(f"  {title} ({len(problems[key])}): {_listed(problems[key], 8)}")

    if project_path:
        todo += _model_checks(lines, project_path, sql_project_path, reports_path, config_path)

    lines += ["", "Nothing to fix." if not todo else
              f"{todo} item(s) marked [!] to look at: the guide is in get_lineage_config_guide."]
    return shape_lines("\n".join(lines), MAX_TOOL_OUTPUT_CHARS,
                       note="the lists above are cut, ask for a single object with get_sql_project_lineage")


def _file_handoffs(lineage) -> dict[str, list[str]]:
    """Objects whose files a similarly named object may have written, with the files they read."""
    readers = lineage.checks["file_readers"]
    if not readers:
        return {}
    from mcp_data_platform_toolkit.lineage.suggestions import suggest_rules
    return {label: readers[label] for label in suggest_rules(lineage)["handoffs"]}


def _model_checks(lines, project_path, sql_project_path, reports_path, config_path) -> int:
    """The Power BI side of the check; returns the number of items to look at."""
    e2e = _load_end_to_end(project_path, sql_project_path, reports_path, config_path)
    checks, problems = e2e.checks, e2e.model.problems
    todo = 0
    lines += ["", f"Semantic model: {e2e.name}", f"  {_coverage_line(e2e)}"]
    if checks["databases"]:
        lines.append("  Databases read: " + ", ".join(
            f"{database or '?'} on {server or '?'}" for server, database in checks["databases"]))
    if checks["reads_other_layers"]:
        todo += 1
        lines.append(f"  [!] Model tables reading from a layer other than '{checks['last_layer']}': "
                     + _listed(checks["reads_other_layers"], 10))
    if checks["not_in_project"]:
        todo += 1
        lines.append("  [!] Model sources not in the database project (another database, a typo, "
                     "an object not yet in the project): " + _listed(checks["not_in_project"], 10))
    if problems.get("not_traced"):
        todo += 1
        lines.append(f"  [!] Model columns whose source is not traced ({len(problems['not_traced'])}): "
                     + _listed(problems["not_traced"], 8))
    if problems.get("native_queries"):
        todo += 1
        lines.append("  [!] Native queries: " + _listed(problems["native_queries"], 5))
    if checks["other_partitions"]:
        lines.append("  Partitions of a kind not read: " + _listed(checks["other_partitions"], 8))
    if problems.get("dax_unresolved"):
        lines.append(f"  DAX references not resolved ({len(problems['dax_unresolved'])}): "
                     + _listed(problems["dax_unresolved"], 5))
    if checks["visuals_without_source"]:
        lines.append(f"  Visuals that reach no column outside the model "
                     f"({len(checks['visuals_without_source'])}, e.g. constants or generated data): "
                     + _listed(checks["visuals_without_source"], 8))
    if e2e.reports.not_analyzed:
        lines.append("  Reports not analyzed: " + _listed(e2e.reports.not_analyzed, 8))
    if e2e.reports.unreadable:
        lines.append("  Report files not readable: " + _listed(e2e.reports.unreadable, 8))
    return todo


def suggest_lineage_rules(sql_project_path: str, config_path: str = "") -> str:
    """
    Proposes the rules of 'lineage.yaml' for the tables nothing loads in the
    SQL of a database project, inferred from the names of the objects: a
    copy in another schema, a prefix or suffix added, the common start of
    the names generalized. Each rule is tried on the project (objects
    matched, sources found, objects the SQL already loads that it would also
    match) and returned as YAML ready to paste, plus a possible rule for the
    source systems when the first layer is named after them. They are
    proposals: check each one with the user, add it, then run
    test_lineage_config.

    Args:
        sql_project_path: The .sqlproj file or the folder of the database project.
        config_path: Optional configuration file, to use instead of the one
            found (the repository's 'lineage.yaml', else the user's own).
    """
    from mcp_data_platform_toolkit.lineage.suggestions import format_suggestions, suggest_rules
    lineage = _load(sql_project_path, config_path, use_proposed=False)
    header = ["=== SUGGESTED LINEAGE RULES ===", f"Database project: {lineage.project.sqlproj}",
              _config_line(lineage)]
    return shape_lines(format_suggestions(suggest_rules(lineage), header), MAX_TOOL_OUTPUT_CHARS,
                       note="ask again after adding the first rules")


# ---------------------------------------------------------------------------
# Drafting the configuration
# ---------------------------------------------------------------------------

def draft_lineage_config(
    sql_project_path: str,
    answers: dict[str, str | bool | list[str]] | None = None,
    project_path: str = "",
) -> str:
    """
    Drafts 'lineage.yaml' for a database project by analyzing its SQL, and
    returns the questions whose answers complete it. USE IT when the user
    wants to create the lineage configuration: it proposes every section
    (layers of the schemas in flow order, ignored schemas, rules for the
    tables nothing loads, ETL columns) from the flows between schemas and
    the object names, and asks only what stays uncertain, each question
    with a proposed answer.

    How to use it: call it without 'answers'; ask the user the open
    questions (all in one message, with the proposed answers); call it again
    with every answer given so far, until no question is open (unanswered
    questions keep the proposed answer). Then save the file with
    save_lineage_config (full access mode): it goes in the user's own
    folder, outside the repository; its answer carries a warning to pass on
    to the user: the file is a draft, to check before relying on it. In
    read-only mode give it to the user, who opens their file with
    'mcp-data-platform-toolkit --lineage-config' (with the same warning).
    It never writes anything. The file says 'draft: true' until the user
    checked it.

    Args:
        sql_project_path: The .sqlproj file or the folder of the database project.
        answers: The user's answers, by the key of each question:
            'schema.<schema>': a layer (bronze, silver, gold or a new one) or 'ignore';
            'layer_order': the layers in flow order, e.g. 'bronze, silver, gold, semantic';
            'rule.<name>': 'yes' or 'no';
            'load.<schema.table>': its source 'schema.object', 'external:<system.table>'
            or 'none'; 'technical_columns': 'yes', 'no' or the patterns to keep;
            'default_schema': the schema of names written without one.
        project_path: Optional Power BI project (.pbip, its folder or the
            .SemanticModel folder) fed by the database: the schemas its model
            reads are proposed as the last layer.
    """
    try:
        from mcp_data_platform_toolkit.lineage.config_draft import draft_config, format_draft
    except ImportError as exc:
        raise ToolError(
            f"The lineage needs the '{exc.name}' package, which is not installed in the "
            f"Python environment of the server. Reinstall it: pip install --upgrade mcp-data-platform-toolkit") from exc
    draft = draft_config(sql_project_path, answers, project_path)
    # The file must arrive whole: the schema table and the question details go first.
    for schemas, details in ((True, True), (False, True), (False, False)):
        text = format_draft(draft, include_schemas=schemas, include_details=details)
        if len(text) <= MAX_TOOL_OUTPUT_CHARS:
            return text
    return shape_lines(text, MAX_TOOL_OUTPUT_CHARS, note="answer the questions shown, then ask again")


# In the answer of a save without a form, for the assistant to pass on to the user as it is.
SAVED_WARNING = (
    "Tell the user, as it is: " + draft_notice.DRAFT_WARNING + " " + draft_notice.HOW_TO_CHECK
    + ' They can open the file in their editor with: mcp-data-platform-toolkit --lineage-config "{sqlproj}"'
)


def _saved_summary(sql_project_path: str, path: str, text: str) -> list[str]:
    """What the saved file does to the lineage, for the user to see at once what it leaves out."""
    import logging
    try:
        lineage = _lineage_with(sql_project_path, path, text)
    except Exception as exc:  # noqa: BLE001 - the file is saved; the check is a courtesy
        logging.getLogger(__name__).warning("The saved lineage configuration could not be checked", exc_info=True)
        return [f"It could not be checked against the project yet ({type(exc).__name__}: {exc}): "
                f"run test_lineage_config."]
    checks = lineage.checks
    layers = {name: len(objects) for name, objects in checks["by_layer"].items()}
    unassigned = {schema: len(names) for schema, names in checks["unassigned"].items()}
    kept = sum(layers.values())
    total = kept + sum(checks["ignored"].values()) + sum(unassigned.values())
    parts = [f"{name} {count}" for name, count in layers.items()]
    if checks["ignored"]:
        parts.append("ignored: " + ", ".join(f"{s} ({n})" for s, n in sorted(checks["ignored"].items())))
    if unassigned:
        parts.append("in no layer: " + ", ".join(f"{s} ({n})" for s, n in sorted(unassigned.items())))
    lines = [f"With this file the lineage has {kept} of {total} objects and "
             f"{len(lineage.objects.edges)} links" + (f" ({'; '.join(parts)})." if parts else ".")]
    if total and kept * 2 < total:
        lines.append(f"ATTENTION: it leaves out {total - kept} of {total} objects: check 'ignore' and "
                     f"the layers, the lineage is mostly empty.")
    items = _attention_items(lineage)
    if items:
        lines.append(f"{len(items)} item(s) to look at: {'; '.join(items)}. test_lineage_config gives the details.")
    else:
        lines.append("test_lineage_config finds nothing to fix. It can't tell whether the layers are the "
                     "right ones: that is for the user to check.")
    return lines


def _attention_items(lineage) -> list[str]:
    """
    The items test_lineage_config marks [!] for the file itself, one line each;
    their number is the one it ends with (a test keeps the two together).
    """
    checks, analysis = lineage.checks, lineage.analysis
    items = []
    if checks["unassigned"]:
        schemas = sorted(checks["unassigned"])
        items.append(f"schemas in no layer ({_listed(schemas, 5)})")
    for rule in lineage.config.rules:
        entry = analysis.rules.get(rule.name, {})
        if not entry.get("matched"):
            items.append(f"rule '{rule.name}' matches no object")
        elif entry.get("missing"):
            items.append(f"rule '{rule.name}': its source is not in the project")
    if checks["loads_not_found"]:
        items.append(f"{len(checks['loads_not_found'])} table(s) nothing loads in the SQL")
    if checks["backward"]:
        items.append(f"{len(checks['backward'])} flow(s) going backwards between layers")
    return items


def _lineage_with(sql_project_path: str, path: str, text: str):
    """
    The lineage seen through the saved file. Without rules, only the layers and
    the ignored schemas differ from the lineage read without configuration,
    which is cached: the SQL is not read again (see `with_layers`).
    """
    from mcp_data_platform_toolkit.sql_project.config import parse_config
    from mcp_data_platform_toolkit.sql_project.project import load_unconfigured_lineage, with_layers
    config = parse_config(text, path)
    if config.rules:
        return _load(sql_project_path, path)
    return with_layers(load_unconfigured_lineage(sql_project_path, config.default_schema), config)


def save_lineage_config(sql_project_path: str, content: str, overwrite: bool = False,
                        scope: str = "project", choice: FirstChoice = None,
                        review: LineageReview = None) -> str:
    """
    Saves the lineage configuration of a database project: the file drafted
    by draft_lineage_config once the user answered its questions, or one
    edited by hand. The content is checked first: if it has problems nothing
    is written and they are listed with their line. An existing file is
    replaced only with 'overwrite'. The answer says how many objects the file
    leaves in the lineage. A file generated by draft_lineage_config is only a
    draft and says 'draft: true': keep that line as it is, also when you
    change the file (only the user removes it, once they checked the file).
    Saving a draft shows the user a warning in a form of the client, with
    the choice to open the file in their editor; the file is saved whatever
    they answer. When the client can't show the form, or the user didn't
    answer it, the answer carries the warning for you to tell the user as it
    is. Until the line is removed, every lineage answer says the
    configuration is a draft. A file written by hand gets no warning.

    Args:
        sql_project_path: The .sqlproj file or the folder of the database project.
        content: The whole text of the file.
        overwrite: Replace the existing configuration.
        scope: 'project' (the default): the user's own configuration, kept in
            the user's folder, outside the repository. 'repo': 'lineage.yaml'
            next to the .sqlproj, in the repository, shared with the team
            through Git: ONLY when the user explicitly asks to share it. When
            the repository has one, it wins over the user's own.
    """
    import os

    from mcp_data_platform_toolkit.mcp_common import user_folder
    from mcp_data_platform_toolkit.mcp_common.folder_choice import settle_first_choice
    from mcp_data_platform_toolkit.semantic_model.project_paths import check_allowed_root
    from mcp_data_platform_toolkit.sql_project.config import (CONFIG_FILE_NAMES, ConfigError, find_config_file,
                                                              parse_config, personal_config_file)
    from mcp_data_platform_toolkit.sql_project.sql_files import find_database_project
    if scope not in ("project", "repo"):
        raise ValueError(f"'scope' must be 'project' or 'repo' (got '{scope}').")
    project = find_database_project(sql_project_path)
    text = draft_notice.unfenced(content)      # copied with the fence of the draft
    if not text.strip():
        raise ValueError("'content' is empty: give the whole text of lineage.yaml "
                         "(draft_lineage_config drafts it).")
    shared = find_config_file(project.folder)
    try:
        parse_config(text, "lineage.yaml")
    except ConfigError as exc:
        raise ValueError(f"Not saved. {exc}") from None
    notice = ""
    if scope == "repo":
        existing = shared
        path = shared or os.path.join(project.folder, CONFIG_FILE_NAMES[0])
        check_allowed_root(path, "The configuration file")
    else:
        # After the checks: the first-time choice of the folder is said in
        # the answer, so it must not be lost with the error of a bad file.
        notice = settle_first_choice(choice)
        path = personal_config_file(project.folder, project.sqlproj)
        existing = path if os.path.isfile(path) else ""
    if existing and not overwrite:
        raise FileExistsError(f"'{existing}' already exists: pass 'overwrite' to replace it.")
    user_folder.write_text(Path(path), text + "\n")
    whose = ("the repository's, shared with the team" if scope == "repo"
             else "yours, outside the repository")
    lines = [f"Saved '{path}'{' (replaced)' if existing else ''}: {whose}."]
    lines += _saved_summary(sql_project_path, path, text)
    if draft_notice.is_generated(text):
        answer = getattr(review, "data", None)
        if isinstance(answer, config_review.ReviewAnswer):
            lines.append("The user was shown the warning that it is only a draft, to check before "
                         "relying on it.")
            if answer.open_in_editor:
                from mcp_data_platform_toolkit.rules_cli import open_in_editor
                lines.append("Opened in an editor on the computer where the server runs." if open_in_editor(Path(path))
                             else "No editor could be opened here: the file is at the path above.")
        else:       # no form, or the user declined it or didn't answer: they may not have seen the warning
            lines.append(SAVED_WARNING.format(sqlproj=project.sqlproj))
    if scope == "project" and shared:
        lines.append(f"Note: the repository has its own '{shared}', which wins over yours: yours is "
                     f"used only if that one is removed.")
    if notice:
        lines.append(notice)
    lines.append("Next: test_lineage_config, to see what the file still doesn't describe.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

NO_PROJECT_MESSAGE = ("Give 'project_path' for the end-to-end lineage of a Power BI project, or "
                      "'sql_project_path' for the lineage of a database project.")

# The extensions a saved export may have, by format: an export is text to
# read, never a file another program would run ('.bat', '.ps1').
EXPORT_EXTENSIONS = {
    "mermaid": (".mmd", ".mermaid", ".md"),
    "markdown": (".md", ".markdown"),
    "csv": (".csv",),
    "json": (".json",),
}


def _export_folders(project_path: str, sql_project_path: str) -> list[str]:
    """
    The folders an export may be saved in when MCP_ALLOWED_ROOTS is not set:
    the Power BI project's (the folder of its '.SemanticModel', where the
    .pbip lives) and the database project's (the folder of its .sqlproj).
    """
    import os

    from mcp_data_platform_toolkit.semantic_model.project_paths import find_semantic_model_folder
    from mcp_data_platform_toolkit.sql_project.sql_files import find_database_project
    folders = []
    if project_path:
        folders.append(os.path.dirname(find_semantic_model_folder(project_path)))
    if sql_project_path:
        folders.append(find_database_project(sql_project_path).folder)
    if not folders:
        raise ValueError(NO_PROJECT_MESSAGE)
    return folders


def _export_selection(sql_project_path, project_path, target, direction, level, depth, reports_path,
                      config_path):
    """(selected graph, title, header lines) for an export."""
    direction = _choice(direction, DIRECTIONS, "direction", "both")
    level = _choice(level, LEVELS, "level", "object")
    try:
        depth = max(int(depth or 0), 0)
    except (TypeError, ValueError):
        raise ValueError(f"'depth' must be a whole number (got '{depth}').") from None
    if project_path:
        from mcp_data_platform_toolkit.lineage.end_to_end import resolve_target
        e2e = _load_end_to_end(project_path, sql_project_path, reports_path, config_path)
        roots = resolve_target(e2e, target, level) if target and target.strip() else []
        graph = e2e.columns if level == "column" else e2e.objects
        title = f"End-to-end lineage of {e2e.name}"
        header = [_coverage_line(e2e)]
        if e2e.sql is not None:
            header.append(f"Database project: {e2e.sql.project.name}")
            if e2e.sql.config.provisional:
                header.append(_config_line(e2e.sql))
        if e2e.reports.analyzed:
            header.append("Reports analyzed: " + ", ".join(e2e.reports.analyzed))
        header.extend(_not_analyzed_lines(e2e))
    elif sql_project_path:
        lineage = _load(sql_project_path, config_path)
        roots = []
        if target and target.strip():
            level, roots = _resolve_target(lineage, target, level)
        graph = lineage.columns if level == "column" else lineage.objects
        title = f"Lineage of the database project {lineage.project.name}"
        header = [_config_line(lineage)]
    else:
        raise ValueError(NO_PROJECT_MESSAGE)
    selected = graph.select(roots, direction, depth) if roots else graph
    header = [f"Level: {level}"] + ([f"Target: {target.strip()}, direction {direction}"
                                     + (f", depth {depth}" if depth else "")] if roots else []) + header
    header.append(f"{len(selected.nodes)} nodes, {len(selected.edges)} edges")
    return selected, title, header


def export_lineage(
    export_format: str = "mermaid",
    project_path: str = "",
    sql_project_path: str = "",
    target: str = "",
    direction: str = "both",
    level: str = "object",
    depth: int = 0,
    reports_path: str = "",
    config_path: str = "",
    structure: ModelStructure = None,
) -> str:
    """
    Exports a lineage as text to keep or share: 'mermaid' (a flowchart with a
    box per layer, drawn by GitHub, Azure DevOps and most Markdown viewers),
    'markdown' (the diagram plus the table of the edges), 'csv' (one row per
    edge) or 'json'. With 'project_path' the end-to-end lineage of a Power BI
    project (plus 'sql_project_path' to include the database); with only
    'sql_project_path' the database project. A diagram is never cut: when it
    doesn't fit a response, narrow it with 'target', 'direction' or 'depth',
    or save it with save_lineage_export (full access mode).

    Args:
        export_format: 'mermaid', 'markdown', 'csv' or 'json'.
        project_path: The .pbip file or folder, for the end-to-end lineage.
        sql_project_path: The database project (.sqlproj or its folder).
        target: Optional starting point, as in get_end_to_end_lineage or
            get_sql_project_lineage.
        direction: 'upstream', 'downstream' or 'both'.
        level: 'object' (the default: readable diagrams) or 'column'.
        depth: Steps to follow from the target (0 = all).
        reports_path: Optional folder with the reports.
        config_path: Optional 'lineage.yaml'.
    """
    from mcp_data_platform_toolkit.lineage.export import FORMATS as EXPORT_FORMATS
    from mcp_data_platform_toolkit.lineage.export import export
    export_format = _choice(export_format, EXPORT_FORMATS, "export_format", "mermaid")
    stop, notice = settle_structure(sql_project_path, project_path, config_path, structure)
    if stop:
        return stop
    selected, title, header = _export_selection(sql_project_path, project_path, target, direction,
                                                level, depth, reports_path, config_path)
    text = export(selected, export_format, title, header)
    if len(text) > MAX_TOOL_OUTPUT_CHARS:
        return _noticed(notice, f"The {export_format} export is {len(text)} characters "
                                f"({len(selected.nodes)} nodes, {len(selected.edges)} edges), more than a "
                                f"response holds ({MAX_TOOL_OUTPUT_CHARS}). Narrow it with 'target', "
                                f"'direction' or 'depth', use level 'object', or save the whole export "
                                f"to a file with save_lineage_export (full access mode).")
    return _noticed(notice, text)


def save_lineage_export(
    output_path: str,
    export_format: str = "markdown",
    project_path: str = "",
    sql_project_path: str = "",
    target: str = "",
    direction: str = "both",
    level: str = "object",
    depth: int = 0,
    reports_path: str = "",
    config_path: str = "",
    overwrite: bool = False,
) -> str:
    """
    Saves a whole lineage export to a file, whatever its size: same formats
    and arguments as export_lineage. The file goes inside the folder of the
    Power BI project (where its .pbip is) or of the database project (where
    its .sqlproj is), or inside MCP_ALLOWED_ROOTS when that is set, with the
    extension of its format: '.md' for markdown, '.mmd' or '.md' for mermaid,
    '.csv', '.json'. The folder must exist; an existing file is replaced only
    with 'overwrite'.

    Args:
        output_path: The file to write (e.g. 'C:\\repo\\docs\\lineage.md').
        export_format: 'markdown' (the default), 'mermaid', 'csv' or 'json'.
        project_path: The .pbip file or folder, for the end-to-end lineage.
        sql_project_path: The database project (.sqlproj or its folder).
        target: Optional starting point.
        direction: 'upstream', 'downstream' or 'both'.
        level: 'object' or 'column'.
        depth: Steps to follow from the target (0 = all).
        reports_path: Optional folder with the reports.
        config_path: Optional 'lineage.yaml'.
        overwrite: Replace the file if it exists.
    """
    import os

    from mcp_data_platform_toolkit.lineage.export import FORMATS as EXPORT_FORMATS
    from mcp_data_platform_toolkit.lineage.export import export
    from mcp_data_platform_toolkit.semantic_model.project_paths import check_output_path
    from mcp_data_platform_toolkit.semantic_model.tmdl_io import write_tmdl_lines
    export_format = _choice(export_format, EXPORT_FORMATS, "export_format", "markdown")
    path = os.path.abspath(str(output_path or "").strip())
    if not str(output_path or "").strip():
        raise ValueError("'output_path' cannot be empty.")
    extensions = EXPORT_EXTENSIONS[export_format]
    if os.path.splitext(path)[1].lower() not in extensions:
        raise ValueError(f"A {export_format} export is saved as {' or '.join(extensions)}: "
                         f"'{os.path.basename(path)}' has another extension.")
    check_output_path(path, _export_folders(project_path, sql_project_path), "The export file")
    if not os.path.isdir(os.path.dirname(path)):
        raise FileNotFoundError(f"The folder of '{path}' does not exist.")
    if os.path.isdir(path):
        raise ValueError(f"'{path}' is a folder: give the file to write.")
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(f"'{path}' already exists: pass 'overwrite' to replace it.")
    selected, title, header = _export_selection(sql_project_path, project_path, target, direction,
                                                level, depth, reports_path, config_path)
    text = export(selected, export_format, title, header)
    write_tmdl_lines(path, text.splitlines(keepends=True))
    return (f"Saved the {export_format} export ({len(selected.nodes)} nodes, {len(selected.edges)} "
            f"edges, {len(text)} characters) to '{path}'.")


def get_lineage_config_guide() -> str:
    """
    Returns the guide to write 'lineage.yaml', the file that tells the
    lineage how a database project realizes the medallion architecture:
    which schemas form each layer and how the loads the SQL doesn't show
    work. USE IT before helping a user write or change that file, then check
    the result with test_lineage_config. To create the file, start from
    draft_lineage_config instead.
    """
    try:
        text = GUIDE_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"Guide not available: cannot read '{GUIDE_FILE}' "
                        f"({exc.__class__.__name__}). The file is part of the package: reinstall it.") from exc
    return text.strip()


def register_lineage_tools(mcp):
    """Registers the lineage tools (read-only) on the MCP server."""
    register_tools(mcp, {
        "get_sql_project_lineage": get_sql_project_lineage,
        "get_end_to_end_lineage": get_end_to_end_lineage,
        "test_lineage_config": test_lineage_config,
        "suggest_lineage_rules": suggest_lineage_rules,
        "draft_lineage_config": draft_lineage_config,
        "export_lineage": export_lineage,
        "get_lineage_config_guide": get_lineage_config_guide,
    })


def register_lineage_writer_tools(mcp):
    """Registers the lineage tools that write files (full access mode only)."""
    register_tools(mcp, {"save_lineage_export": save_lineage_export,
                         "save_lineage_config": save_lineage_config})
