"""
The lineage of a database project as two graphs (objects, columns) with
their medallion layers, cached, plus the checks that tell whether the
configuration describes the project.

The checks are what makes the configuration writable by trial: each one
points at something the file says wrong or doesn't say yet (a schema in no
layer, a table nothing loads, a rule that matches nothing, a flow going
backwards from a later layer to an earlier one).
"""

from __future__ import annotations

import dataclasses
import os
import threading
from dataclasses import dataclass, field

from mcp_data_platform_toolkit.lineage.graph import CONFIDENCE_CERTAIN, CONFIDENCE_RULE, Graph, GraphEdge, Node
from mcp_data_platform_toolkit.semantic_model.project_paths import check_allowed_root
from mcp_data_platform_toolkit.semantic_model.tmdl_io import file_fingerprint
from mcp_data_platform_toolkit.sql_project.analyzer import Analysis, analyze
from mcp_data_platform_toolkit.sql_project.config import (SOURCE_LAYER, LineageConfig, find_config_file,
                                                          find_project_config, load_config, parse_config)
from mcp_data_platform_toolkit.sql_project.sql_files import DatabaseProject, discover_sql_files, find_database_project

DATA_KINDS = ("table", "view", "external_table", "function")
MATERIALIZED_KINDS = ("table", "external_table")
OUTSIDE_KINDS = ("file", "source")


@dataclass
class SqlLineage:
    project: DatabaseProject
    config: LineageConfig
    analysis: Analysis
    excluded_files: list[str]
    objects: Graph
    columns: Graph
    checks: dict = field(default_factory=dict)


_cache: dict[tuple, tuple] = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 3


# What the lineage tools learned about a project during this session, kept in memory:
# the structure they proposed for a project that has no configuration (shown as
# provisional until the user saves it), and the projects whose user was already asked.
_provisional: dict[str, str] = {}
_asked: set[str] = set()


def _project_key(sqlproj: str) -> str:
    return os.path.normcase(os.path.abspath(sqlproj))


def provisional_config(sqlproj: str) -> str:
    """The text of the structure proposed for the project in this session, or ''."""
    with _cache_lock:
        return _provisional.get(_project_key(sqlproj), "")


def set_provisional_config(sqlproj: str, text: str) -> None:
    with _cache_lock:
        _provisional[_project_key(sqlproj)] = text


def forget_provisional_config(sqlproj: str) -> None:
    with _cache_lock:
        _provisional.pop(_project_key(sqlproj), None)


def structure_asked(sqlproj: str) -> bool:
    """True when the user was already asked, in this session, about the project's structure."""
    with _cache_lock:
        return _project_key(sqlproj) in _asked


def mark_structure_asked(sqlproj: str) -> None:
    with _cache_lock:
        _asked.add(_project_key(sqlproj))


def layer_coverage(lineage: SqlLineage) -> tuple[int, int]:
    """(objects placed in a layer, objects in total) of the project; ignored schemas are left out."""
    checks = lineage.checks
    placed = sum(len(objects) for objects in checks["by_layer"].values())
    return placed, placed + sum(len(objects) for objects in checks["unassigned"].values())


def clear_sql_cache() -> None:
    """Invalidates the cached lineages and what was learned in the session (used by tests)."""
    with _cache_lock:
        _cache.clear()
        _provisional.clear()
        _asked.clear()


def load_sql_lineage(sql_project_path: str, config_path: str = "",
                     use_proposed: bool = True) -> SqlLineage:
    """
    The lineage of the project, reusing the cached one while no file of the
    project and no configuration changed (fingerprint: mtime + size).

    A project with no configuration (or with one that says nothing, the
    standard layers and nothing else) is read through the structure the
    lineage tools proposed for it in this session, when there is one: its
    config is marked `provisional`. `use_proposed=False` reads the files as
    they are, for the tools that check them.
    """
    project = find_database_project(sql_project_path)
    if config_path:
        config_file = os.path.abspath(config_path)
        if not os.path.isfile(config_file):
            raise FileNotFoundError(f"Configuration file '{config_path}' not found.")
        check_allowed_root(config_file, "The configuration file")
    else:
        config_file = find_project_config(project.folder, project.sqlproj)

    files, excluded = discover_sql_files(project)
    tracked = [f.path for f in files] + [project.sqlproj] + ([config_file] if config_file else [])
    fingerprint = tuple((path, file_fingerprint(path)) for path in tracked)
    proposed = ""
    if use_proposed and not config_path:
        proposed = provisional_config(project.sqlproj)
    key = (os.path.normcase(project.sqlproj), os.path.normcase(config_file), hash(proposed))

    def compute() -> SqlLineage:
        config = load_config(config_file)
        # The repository's file is the team's: never replaced. The user's own, when it says nothing, is.
        shared = find_config_file(project.folder)
        if proposed and config.blank and not (shared and config_file == shared):
            config = dataclasses.replace(parse_config(proposed, "lineage.yaml (proposed)"), provisional=True)
        return _analyze(project, config, files, excluded)
    return _cached(key, fingerprint, compute)


def load_unconfigured_lineage(sql_project_path: str, default_schema: str) -> SqlLineage:
    """
    The lineage of the project read without any configuration: no layers,
    no rules, names without schema in `default_schema`. A draft of the
    configuration starts from it, and `with_layers` shows it through the
    proposed layers without reading the SQL again. Cached as load_sql_lineage.
    """
    project = find_database_project(sql_project_path)
    files, excluded = discover_sql_files(project)
    tracked = [f.path for f in files] + [project.sqlproj]
    fingerprint = tuple((path, file_fingerprint(path)) for path in tracked)
    key = (os.path.normcase(project.sqlproj), "", default_schema.lower())
    config = LineageConfig(path="", layers=(), default_schema=default_schema)
    return _cached(key, fingerprint, lambda: _analyze(project, config, files, excluded))


def with_layers(lineage: SqlLineage, config: LineageConfig) -> SqlLineage:
    """
    The same analysis seen through the layers and ignored schemas of
    `config`. Only those can differ: the default schema and the rules
    change what the SQL analysis finds, so `config` must have the ones the
    analysis was made with.
    """
    return _build(lineage.project, config, lineage.analysis, lineage.excluded_files)


def _analyze(project, config: LineageConfig, files, excluded) -> SqlLineage:
    analysis = analyze(files, config)
    for rel in excluded:
        analysis.problem("excluded_files", rel)
    return _build(project, config, analysis, excluded)


def _cached(key: tuple, fingerprint: tuple, compute) -> SqlLineage:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry[0] == fingerprint:
            return entry[1]
    result = compute()
    with _cache_lock:
        _cache[key] = (fingerprint, result)
        while len(_cache) > _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
    return result


def column_label(obj, column: str) -> str:
    """'schema.object.Column'; for a file 'file:path (Column)', since a path has dots of its own."""
    return f"{obj.label} ({column})" if obj.kind == "file" else f"{obj.label}.{column}"


def _build(project, config: LineageConfig, analysis: Analysis, excluded: list[str]) -> SqlLineage:
    objects = analysis.objects
    ignored = {key for key, obj in objects.items() if config.is_ignored(obj.schema)}

    def layer(obj) -> str:
        if obj.kind in OUTSIDE_KINDS:
            return SOURCE_LAYER
        # An object the SQL reads but the project doesn't define (a synonym, another database)
        # in a schema of no layer is outside the project, like a file: a source.
        return config.layer_of(obj.schema) or (SOURCE_LAYER if not obj.in_project else "")

    object_nodes: dict[str, Node] = {}
    column_nodes: dict[str, Node] = {}
    for key, obj in objects.items():
        if key in ignored:
            continue
        flags = [obj.flag] if obj.flag else []
        where = f"{obj.file}:{obj.line}" if obj.file else ""
        object_nodes[key] = Node(key, obj.label, layer(obj), obj.kind, flags, where)
        for column in obj.columns or []:
            column_flags = list(flags)
            if config.is_technical(column):
                column_flags.append("technical")
            ckey = f"{key}|{column.lower()}"
            column_nodes[ckey] = Node(ckey, column_label(obj, column), layer(obj), "column",
                                      column_flags, where)

    def graph_edges(edges, nodes, of) -> list[GraphEdge]:
        result = []
        for edge in edges:
            if of(edge.source) in ignored or of(edge.target) in ignored:
                continue
            for end in (edge.source, edge.target):
                if end not in nodes:            # a column the catalog didn't list
                    owner = objects.get(of(end))
                    label = column_label(owner, end.split("|", 1)[1]) if owner else end
                    nodes[end] = Node(end, label, layer(owner) if owner else "", "column",
                                      [owner.flag] if owner and owner.flag else [])
            result.append(GraphEdge(edge.source, edge.target, edge.kind, edge.evidence,
                                    CONFIDENCE_RULE if edge.rule else CONFIDENCE_CERTAIN, edge.rule))
        return result

    layers = config.layer_names
    object_graph = Graph(object_nodes, graph_edges(analysis.object_edges, object_nodes, lambda k: k),
                         layers)
    column_graph = Graph(column_nodes, graph_edges(analysis.column_edges, column_nodes,
                                                   lambda k: k.split("|", 1)[0]), layers)
    result = SqlLineage(project, config, analysis, excluded, object_graph, column_graph)
    result.checks = _checks(result, ignored)
    return result


def lookup_pairs(objects: dict, object_edges, column_edges) -> set[tuple[str, str]]:
    """
    The (source schema, target schema) pairs, lowercase, that are lookups: a
    staging table taking the keys of a dimension. Data flows both ways
    between the two schemas, this direction only UPDATEs rows that exist (no
    view, INSERT or MERGE builds anything from it) and it moves fewer columns
    (one key each, however many tables do it). The other direction is where
    the data comes from. A tie is no lookup.
    """
    def schema_of(key: str) -> str:
        obj = objects.get(key.split("|", 1)[0])
        return obj.schema.lower() if obj is not None and obj.in_project else ""

    kinds: dict[tuple[str, str], set] = {}
    for edge in object_edges:
        source, target = schema_of(edge.source), schema_of(edge.target)
        if source and target and source != target:
            kinds.setdefault((source, target), set()).add(edge.kind)
    columns: dict[tuple[str, str], int] = {}
    for edge in column_edges:
        pair = (schema_of(edge.source), schema_of(edge.target))
        columns[pair] = columns.get(pair, 0) + 1
    return {(s, t) for (s, t), seen in kinds.items()
            if seen == {"update"} and (t, s) in kinds
            and columns.get((s, t), 0) < columns.get((t, s), 0)}


def _checks(lineage: SqlLineage, ignored: set[str]) -> dict:
    config = lineage.config
    objects = lineage.analysis.objects
    graph = lineage.objects
    in_project = [o for k, o in objects.items() if o.in_project and o.kind in DATA_KINDS and k not in ignored]

    by_layer: dict[str, list[str]] = {name: [] for name in config.layer_names}
    unassigned: dict[str, list[str]] = {}
    for obj in in_project:
        layer = config.layer_of(obj.schema)
        if layer:
            by_layer[layer].append(obj.label)
        else:
            unassigned.setdefault(obj.schema, []).append(obj.label)

    ignored_count: dict[str, int] = {}
    for key in ignored:
        if objects[key].in_project:
            ignored_count[objects[key].schema] = ignored_count.get(objects[key].schema, 0) + 1

    incoming = graph.incoming()
    first = config.layer_names[0] if config.layer_names else ""
    loads_not_found = sorted(
        o.label for o in in_project
        if o.kind in MATERIALIZED_KINDS and config.layer_of(o.schema) not in ("", first)
        and not incoming.get(o.key))

    # Objects after the first layer whose only sources are files. Either people drop
    # those files (a CSV of targets) or a pipeline writes them from something the SQL
    # doesn't show (a Parquet exported from a view): which one is told by the names,
    # see `suggest_rules`.
    file_readers: dict[str, list[str]] = {}
    for obj in in_project:
        if config.layer_of(obj.schema) in ("", first):
            continue
        sources = incoming.get(obj.key) or []
        if sources and all(graph.nodes[e.source].kind == "file" and not e.rule for e in sources):
            file_readers[obj.label] = sorted({graph.nodes[e.source].label for e in sources})

    backward, lookups = [], []
    lookup = lookup_pairs(objects, graph.edges, lineage.columns.edges)
    for edge in graph.edges:
        source, target = graph.nodes[edge.source], graph.nodes[edge.target]
        si, ti = config.layer_index(source.layer), config.layer_index(target.layer)
        if si is not None and ti is not None and si > ti:
            line = (f"{source.label} [{source.layer}] -> {target.label} [{target.layer}]"
                    f"  {edge.evidence}")
            pair = (objects[edge.source].schema.lower(), objects[edge.target].schema.lower()) \
                if edge.source in objects and edge.target in objects else None
            (lookups if pair in lookup else backward).append(line)

    column_incoming = lineage.columns.incoming()
    without_source: dict[str, list[str]] = {}
    for obj in in_project:
        layer = config.layer_of(obj.schema)
        if layer in ("", first):
            continue
        for column in obj.columns or []:
            key = f"{obj.key}|{column.lower()}"
            if not column_incoming.get(key) and not config.is_technical(column):
                without_source.setdefault(layer, []).append(f"{obj.label}.{column}")

    return {
        "by_layer": by_layer,
        "unassigned": unassigned,
        "ignored": ignored_count,
        "loads_not_found": loads_not_found,
        "file_readers": file_readers,
        "backward": sorted(backward),
        "lookups": sorted(lookups),
        "columns_without_source": without_source,
    }
