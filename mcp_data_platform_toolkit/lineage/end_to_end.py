"""
The end-to-end lineage: from the source column, through the medallion
layers of the database project, Power Query, the columns and measures of the
semantic model, to the visuals, filters and bookmarks of the reports.

Three parts are joined on shared nodes:

  - the column graph of the database project (`sql_project`), when one is
    given: without it the columns the model reads are still nodes, in a
    'database' layer of their own;
  - the semantic model (`model_lineage`);
  - the PBIR reports bound to the model (`report_lineage`).

The column graph is the lineage itself. The object graph is the same graph
one level up (columns by their table or view, visuals by their page) for an
overview.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from mcp_data_platform_toolkit.lineage.graph import Graph, project
from mcp_data_platform_toolkit.lineage.model_lineage import MODEL_LAYER, ModelPart, build_model_part, column_id, measure_id, table_id
from mcp_data_platform_toolkit.lineage.report_lineage import REPORT_LAYER, ReportPart, build_report_part
from mcp_data_platform_toolkit.semantic_model.project_paths import find_semantic_model_folder

COLUMN_KINDS = {"column", "calculated column", "measure", "external column", "visual", "filter",
                "bookmark", "report measure"}
OUTSIDE_LAYERS = {"source", "database"}


@dataclass
class EndToEnd:
    name: str
    columns: Graph
    objects: Graph
    sql: object
    model: ModelPart
    reports: ReportPart
    checks: dict = field(default_factory=dict)


def build_end_to_end(project_path: str, sql_project_path: str = "", reports_path: str = "",
                     config_path: str = "") -> EndToEnd:
    sql = None
    if sql_project_path:
        from mcp_data_platform_toolkit.sql_project.project import load_sql_lineage
        sql = load_sql_lineage(sql_project_path, config_path)
    model_folder = find_semantic_model_folder(project_path)
    model = build_model_part(project_path, sql)
    report_root = reports_path or os.path.dirname(model_folder)
    reports = build_report_part(report_root, model_folder, model)

    layers = (list(sql.config.layer_names) if sql is not None else ["database"]) + [MODEL_LAYER, REPORT_LAYER]
    nodes = dict(sql.columns.nodes) if sql is not None else {}
    object_nodes = dict(sql.objects.nodes) if sql is not None else {}
    for part in (model, reports):
        for node_id, node in part.nodes.items():
            (nodes if node.kind in COLUMN_KINDS else object_nodes)[node_id] = node
    edges = (list(sql.columns.edges) if sql is not None else []) + model.edges + reports.edges
    columns = Graph(nodes, edges, layers)

    owner = {**model.owners, **reports.owners}
    for node_id in nodes:
        if "|" in node_id and node_id not in owner and not node_id.startswith(("visual:", "filter:")):
            owner[node_id] = node_id.split("|", 1)[0]         # a database column: its object
    for node_id, node in nodes.items():
        if node.kind == "measure":
            object_nodes[node_id] = node
    objects = project(columns, owner, object_nodes)
    if sql is not None:
        seen = {(e.source, e.target) for e in objects.edges}
        objects.edges.extend(e for e in sql.objects.edges if (e.source, e.target) not in seen)

    name = os.path.basename(model_folder)
    name = name[: -len(".SemanticModel")] if name.endswith(".SemanticModel") else name
    result = EndToEnd(name, columns, objects, sql, model, reports)
    result.checks = _checks(result)
    return result


def _checks(e2e: EndToEnd) -> dict:
    sql, model, columns = e2e.sql, e2e.model, e2e.columns
    last = sql.config.layer_names[-1] if sql is not None and sql.config.layer_names else ""
    other_layers, not_in_project = [], []
    for table, read in sorted(model.reads.items()):
        for key, label in sorted(read.items()):
            if sql is None or key.startswith("external:"):
                continue
            node = sql.objects.nodes.get(key)
            if node is None:
                not_in_project.append(f"{table} <- {label}")
            elif node.layer != last:
                other_layers.append(f"{table} <- {label} [{node.layer or 'no layer'}]")

    databases = sorted({(o.server, o.database) for s in model.sources for os_ in s.columns.values()
                        for o in os_ if o.kind in ("db", "native") and (o.server or o.database)})
    # The columns Power Query or Direct Lake fill: calculated tables have no source to trace.
    fed = {column_id(s.table, c) for s in model.sources if s.kind in ("m", "direct_lake") for c in s.columns}
    data_columns = [columns.nodes[c] for c in sorted(fed) if c in columns.nodes]
    traced = [n for n in data_columns if "not traced" not in n.flags]

    # Visuals that reach a column outside the model (database or source): the point of it
    # all. One pass forward from every node outside the model.
    outgoing = columns.outgoing()
    reached = {k for k, n in columns.nodes.items() if n.layer not in (MODEL_LAYER, REPORT_LAYER)}
    frontier = list(reached)
    while frontier:
        node_id = frontier.pop()
        for edge in outgoing.get(node_id, []):
            if edge.target not in reached:
                reached.add(edge.target)
                frontier.append(edge.target)

    visuals = [n for n in columns.nodes.values() if n.kind == "visual"]
    reaching = [v for v in visuals if v.id in reached]
    return {
        "last_layer": last,
        "reads_other_layers": other_layers,
        "not_in_project": not_in_project,
        "databases": databases,
        "data_columns": (len(traced), len(data_columns)),
        "visuals": (len(reaching), len(visuals)),
        "visuals_without_source": sorted(v.label for v in visuals if v not in reaching),
        "other_partitions": sorted(f"{s.table} (partition '{s.partition}')" for s in model.sources
                                   if s.kind == "other"),
    }


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        value = value[1:-1].replace("''", "'")
    return value


def resolve_target(e2e: EndToEnd, target: str, level: str) -> list[str]:
    """
    Root nodes of a target, at the given level:

      '[Measure]', 'Table[Column]' or 'Table[Measure]', 'Table'   the model
      'schema.object', 'schema.object.column'                       the database
      'Report', 'Report/Page', 'Report/Page/Visual'                  the reports
                                                                    (visual title, type or id)
    """
    text = target.strip()
    graph = e2e.columns if level == "column" else e2e.objects

    if text.endswith("]") and "[" in text:
        table, _bracket, name = text[:-1].partition("[")
        table, name = _clean(table), name.strip()
        candidates = []
        if table:
            candidates = [column_id(table, name)]
        candidates.append(measure_id(name))
        for node_id in candidates:
            if node_id in e2e.columns.nodes:
                if level == "object" and node_id.startswith("model:"):
                    return [table_id(table)]
                return [node_id]
        raise LookupError(f"'{text}' is neither a column nor a measure of the model '{e2e.name}'.")

    if "/" in text:
        parts = [p.strip().lower() for p in text.split("/")]
        found = _report_nodes(e2e, parts, level)
        if not found:
            raise LookupError(f"No report, page or visual matches '{text}'. Reports analyzed: "
                              f"{', '.join(e2e.reports.analyzed) or 'none'}.")
        return found

    table = table_id(_clean(text))
    if table in e2e.objects.nodes:
        if level == "object":
            return [table]
        return [k for k, o in e2e.model.owners.items() if o == table and k in e2e.columns.nodes]
    report = f"report:{text.lower()}"
    if report in e2e.objects.nodes:
        return _report_nodes(e2e, [text.lower()], level)

    wanted = ".".join(_clean(p).strip("[]\"").lower() for p in text.split("."))
    if wanted in e2e.objects.nodes:
        if level == "object":
            return [wanted]
        prefix = wanted + "|"
        return [k for k in e2e.columns.nodes if k.startswith(prefix)]
    owner, _dot, column = wanted.rpartition(".")
    if owner and f"{owner}|{column}" in e2e.columns.nodes:
        return [f"{owner}|{column}"] if level == "column" else [owner]
    matches = [k for k, n in graph.nodes.items() if n.label.lower() == text.lower()]
    if matches:
        return matches
    raise LookupError(f"'{text}' is not a column, measure, table, database object or report "
                      f"of the lineage of '{e2e.name}'.")


def _report_nodes(e2e: EndToEnd, parts: list[str], level: str) -> list[str]:
    """Visuals (column level) or pages and reports (object level) matching 'report/page/visual'."""
    report = parts[0]
    page = parts[1] if len(parts) > 1 else None
    visual = parts[2] if len(parts) > 2 else None
    result = []
    for node_id, node in e2e.columns.nodes.items():
        if node.kind not in ("visual", "filter", "bookmark", "report measure"):
            continue
        segments = [s.strip().lower() for s in node.label.split(" / ")]
        if segments[0] != report:
            continue
        if page is not None and (len(segments) < 3 or segments[1] != page):
            continue
        if visual is not None:
            name = segments[-1]
            visual_id = node_id.rsplit("|", 1)[-1]
            if visual not in (name, visual_id) and not name.startswith(f"'{visual}'") \
                    and not name.startswith(f"{visual} "):
                continue
        result.append(node_id)
    if level == "object":
        owners = {e2e.reports.owners.get(n, n) for n in result}
        if page is None:
            return [f"report:{report}"] if f"report:{report}" in e2e.objects.nodes else sorted(owners)
        return sorted(owners)
    return sorted(result)
