"""
The semantic model in the end-to-end lineage: its columns and measures as
nodes, and the edges that reach them:

  - from the database (or another source) to the data columns, through the
    Power Query of each partition, its native queries or Direct Lake;
  - from columns and measures to calculated columns and measures (DAX);
  - from what calculation items read to their calculation group column;
  - from what a calculated table reads to its columns (field parameters
    included).

A database object is identified by 'schema.object' as in the database
project, so the two graphs meet on the same nodes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from mcp_data_platform_toolkit.lineage.graph import GraphEdge, Node
from mcp_data_platform_toolkit.semantic_model import dax_lexer, power_query
from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.dax_lineage import build_model_index, resolve_dax_dependencies
from mcp_data_platform_toolkit.semantic_model.model_loader import load_definition
from mcp_data_platform_toolkit.semantic_model.project_paths import find_semantic_model_folder

MODEL_LAYER = "semantic model"
SOURCE_LAYER = "source"
CONFIDENCE_TO_VERIFY = "to verify"


def column_id(table: str, column: str) -> str:
    return f"model:{table.lower()}[{column.lower()}]"


def measure_id(name: str) -> str:
    return f"measure:{name.lower()}"


def table_id(table: str) -> str:
    return f"table:{table.lower()}"


def db_column_id(schema: str, name: str, column: str) -> str:
    return f"{schema}.{name}".lower() + f"|{column.lower()}"


def _parse_ref(text: str) -> tuple[str, str]:
    """'Sales[Amount]' -> ('Sales', 'Amount')."""
    table, _bracket, rest = text.partition("[")
    return table.strip().strip("'"), rest[:-1] if rest.endswith("]") else rest


@dataclass
class ModelPart:
    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    owners: dict = field(default_factory=dict)          # node id -> object node id (object level)
    sources: list = field(default_factory=list)         # power_query.TableSource
    reads: dict = field(default_factory=dict)           # model table -> {db object key: label}
    problems: dict = field(default_factory=dict)        # category -> [text]
    measure_tables: dict = field(default_factory=dict)  # measure name (lower) -> table
    levels: dict = field(default_factory=dict)          # (table, hierarchy, level) lower -> column

    def problem(self, category: str, text: str) -> None:
        self.problems.setdefault(category, []).append(text)


def _where(project_folder: str, obj) -> str:
    try:
        relative = os.path.relpath(obj.file_path, project_folder)
    except ValueError:
        relative = obj.file_path
    return f"{relative.replace(os.sep, '/')}:{obj.block.start + 1}"


def build_model_part(project_path: str, sql=None) -> ModelPart:
    """
    The model's nodes and edges. `sql` is the database project's lineage
    (`sql_project.project.SqlLineage`) or None: without it the database
    columns are still nodes, in a 'database' layer of their own.
    """
    definition = load_definition(project_path)
    tables = definition["tables"]
    index = build_model_index(tables)
    part = ModelPart()
    folder = find_semantic_model_folder(project_path)

    for table, entry in tables.items():
        tid = table_id(table)
        part.nodes[tid] = Node(tid, table, MODEL_LAYER, "model table")
        for obj in entry["column_objects"]:
            cid = column_id(table, obj.name)
            kind = "calculated column" if obj.block.inline_expression is not None else "column"
            part.nodes[cid] = Node(cid, f"{table}[{obj.name}]", MODEL_LAYER, kind, [], _where(folder, obj))
            part.owners[cid] = tid
        for obj in entry["measures"]:
            mid = measure_id(obj.name)
            part.nodes[mid] = Node(mid, f"[{obj.name}]", MODEL_LAYER, "measure", [], _where(folder, obj))
            part.owners[mid] = mid
            part.measure_tables[obj.name.lower()] = table
        for hierarchy, levels in entry["hierarchies"].items():
            for level, column in levels.items():
                part.levels[(table.lower(), hierarchy.lower(), level.lower())] = column

    _sources(part, project_path, sql, folder)
    _dax(part, tables, index, folder)
    return part


# ---------------------------------------------------------------------------
# Sources of the data columns
# ---------------------------------------------------------------------------

def _sources(part: ModelPart, project_path: str, sql, folder: str) -> None:
    sources = power_query.model_sources(project_path)
    part.sources = sources
    native_cache: dict[str, dict] = {}
    database_layer = "database"
    for source in sources:
        if source.kind not in ("m", "direct_lake"):
            continue
        evidence_base = f"partition '{source.partition}'"
        for column, origins in source.columns.items():
            target = column_id(source.table, column)
            if target not in part.nodes:
                continue
            traced = False
            for origin in origins:
                how = "; ".join(origin.how)
                evidence = f"{evidence_base}" + (f": {how}" if how else "")
                kind = "direct lake" if origin.connector == "Direct Lake" else "power query"
                if origin.kind == "db":
                    sid = _db_node(part, sql, origin.schema or "dbo", origin.name, origin.column,
                                   database_layer)
                    part.edges.append(GraphEdge(sid, target, kind, evidence))
                    part.reads.setdefault(source.table, {})[sid.split("|", 1)[0]] = \
                        f"{origin.schema or 'dbo'}.{origin.name}"
                    traced = True
                elif origin.kind == "native":
                    found = native_cache.get(origin.query)
                    if found is None:
                        found = _native(origin.query, sql, f"{source.table} ({evidence_base})")
                        native_cache[origin.query] = found
                        for problem in found["problems"]:
                            part.problem("native_queries", problem)
                    columns = {k.lower(): v for k, v in found["columns"].items()}
                    for key, source_column in columns.get(origin.column.lower(), []):
                        obj = found["objects"].get(key)
                        schema, name = (obj.schema, obj.name) if obj else key.split(".", 1)
                        sid = _db_node(part, sql, schema, name, source_column, database_layer, obj)
                        part.edges.append(GraphEdge(sid, target, "native query", evidence))
                        part.reads.setdefault(source.table, {})[key] = obj.label if obj else key
                        traced = True
                    if origin.column.lower() not in columns:
                        part.problem("not_traced", f"{source.table}[{column}]: column '{origin.column}' "
                                     f"not found in the native query of {evidence_base}")
                elif origin.kind == "external":
                    sid = f"external:{origin.connector}:{origin.name}".lower() + f"|{origin.column.lower()}"
                    if sid not in part.nodes:
                        part.nodes[sid] = Node(sid, f"{origin.name or origin.connector}.{origin.column}"
                                               f" ({origin.connector})", SOURCE_LAYER, "external column")
                        part.owners[sid] = sid.split("|", 1)[0]
                        owner = part.owners[sid]
                        if owner not in part.nodes:
                            part.nodes[owner] = Node(owner, f"{origin.name or origin.connector} "
                                                     f"({origin.connector})", SOURCE_LAYER, "external source")
                    part.edges.append(GraphEdge(sid, target, kind, evidence))
                    part.reads.setdefault(source.table, {})[part.owners[sid]] = \
                        f"{origin.name or origin.connector} ({origin.connector})"
                    traced = True
                elif origin.kind == "generated":
                    part.nodes[target].flags.append("generated in Power Query")
                    traced = True
                else:
                    part.problem("not_traced", f"{source.table}[{column}]: {origin.reason}")
            if not traced and "not traced" not in part.nodes[target].flags:
                part.nodes[target].flags.append("not traced")


def _db_node(part: ModelPart, sql, schema: str, name: str, column: str, database_layer: str,
             obj=None) -> str:
    """The node of a database column, shared with the database project's graph when there is one."""
    sid = db_column_id(schema, name, column)
    key = sid.split("|", 1)[0]
    if sql is not None and sid in sql.columns.nodes:
        return sid
    if sid in part.nodes:
        return sid
    flags: list[str] = []
    layer = database_layer
    label_object = obj.label if obj is not None else f"{schema}.{name}"
    if sql is not None:
        known = sql.objects.nodes.get(key)
        if known is not None:
            layer = known.layer
            label_object = known.label
            flags = list(known.flags) or ["column not found in the project"]
        else:
            layer = sql.config.layer_of(schema)
            flags = ["not in project"]
    part.nodes[sid] = Node(sid, f"{label_object}.{column}", layer, "column", flags)
    part.owners[sid] = key
    if key not in part.nodes and (sql is None or key not in sql.objects.nodes):
        part.nodes[key] = Node(key, label_object, layer, "object", list(flags[:1]))
    return sid


def _native(query: str, sql, evidence: str) -> dict:
    from mcp_data_platform_toolkit.sql_project.analyzer import query_lineage
    from mcp_data_platform_toolkit.sql_project.config import LineageConfig
    config = sql.config if sql is not None else LineageConfig(path="")
    return query_lineage(query, sql.analysis if sql is not None else None, config, evidence)


# ---------------------------------------------------------------------------
# DAX: calculated columns, measures, calculation items, calculated tables
# ---------------------------------------------------------------------------

def _ref_node(part: ModelPart, reference: str, measure: bool) -> str | None:
    table, name = _parse_ref(reference)
    node = measure_id(name) if measure else column_id(table, name)
    return node if node in part.nodes else None


def _dax(part: ModelPart, tables: dict, index: dict, folder: str) -> None:
    for table, entry in tables.items():
        for obj in entry["column_objects"]:
            if obj.block.inline_expression is None:
                continue
            _depends(part, obj.expression(), table, "column", index,
                     column_id(table, obj.name), "dax", _where(folder, obj))
        for obj in entry["measures"]:
            _depends(part, obj.expression(), table, "measure", index,
                     measure_id(obj.name), "dax", _where(folder, obj))
        if entry["is_calc_group"] and entry["column_objects"]:
            # The column that lists the items is the one bound to their 'Name'.
            listing = next((c for c in entry["column_objects"]
                            if (getattr(tmdl.get_property(c.lines, c.block, "sourceColumn"), "value", "")
                                or "").strip().lower() == "name"), entry["column_objects"][0])
            target = column_id(table, listing.name)
            for item in entry["calc_items"]:
                _depends(part, item.expression(), table, "calculationItem", index, target,
                         "calculation item", f"item '{item.name}' ({_where(folder, item)})")

    for source in part.sources:
        if source.kind != "calculated" or not source.dax:
            continue
        _calculated_table(part, source, tables, index)


def _depends(part, expression, table, kind, index, target, edge_kind, evidence) -> None:
    if not expression:
        return
    resolved = resolve_dax_dependencies(expression, table, kind, index)
    for reference in resolved["columns"]:
        node = _ref_node(part, reference, measure=False)
        if node and node != target:
            part.edges.append(GraphEdge(node, target, edge_kind, evidence))
    for reference in resolved["measures"]:
        node = _ref_node(part, reference, measure=True)
        if node and node != target:
            part.edges.append(GraphEdge(node, target, edge_kind, evidence))
    for unresolved in resolved["unresolved"]:
        part.problem("dax_unresolved", f"{part.nodes[target].label}: {unresolved}")


def _calculated_table(part: ModelPart, source, tables, index) -> None:
    """
    A calculated table reads the columns and measures of its DAX. A field
    parameter (NAMEOF) offers each of them through all its columns. For any
    other calculated table a column takes the referenced column with its
    name, or, when none has it, every referenced column marked 'to verify':
    which one it is can't be read without evaluating the DAX.
    """
    entry = tables.get(source.table)
    if entry is None:
        return
    resolved = resolve_dax_dependencies(source.dax, source.table, "measure", index)
    references = [(r, False) for r in resolved["columns"]] + [(r, True) for r in resolved["measures"]]
    nodes = [(r, _ref_node(part, r, m)) for r, m in references]
    nodes = [(r, n) for r, n in nodes if n]
    field_parameter = "NAMEOF" in {f.upper() for f in dax_lexer.analyze(source.dax).functions}
    evidence = f"calculated table '{source.table}'"
    for obj in entry["column_objects"]:
        target = column_id(source.table, obj.name)
        if not nodes:
            if target in part.nodes and obj.block.inline_expression is None:
                part.nodes[target].flags.append("generated in DAX")     # CALENDARAUTO(), DATATABLE(...)
            continue
        if field_parameter:
            for _reference, node in nodes:
                part.edges.append(GraphEdge(node, target, "field parameter", evidence))
            continue
        same = [n for r, n in nodes if _parse_ref(r)[1].lower() == obj.name.lower()]
        if same:
            for node in same:
                part.edges.append(GraphEdge(node, target, "calculated table", evidence))
        else:
            for _reference, node in nodes:
                part.edges.append(GraphEdge(node, target, "calculated table", evidence,
                                            CONFIDENCE_TO_VERIFY))
