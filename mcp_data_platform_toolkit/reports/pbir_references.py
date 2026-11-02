"""
References to model fields inside PBIR report files (and inside the DAX of
report-level measures), and how a reference matches a searched field:
certain binding, probable textual match, or none.
"""

from __future__ import annotations

from mcp_data_platform_toolkit.semantic_model import dax_lexer


PBIR_TEXT_KEYS = ("queryref", "restatement", "nativereferencename", "displayname")

# Possible outcomes of a reference search in a PBIR file.
MATCH_BINDING = "binding"   # the report really binds that field: certain match
MATCH_TEXT = "text"         # only a textual match: probable, to be verified
MATCH_NONE = ""


def _from_alias_map(node: dict) -> dict:
    """
    Alias -> entity declared in the 'From' clause of a PBIR query.
    Needed because about one reference in eight doesn't name the table
    directly ({"SourceRef": {"Entity": ...}}) but through an alias
    ({"SourceRef": {"Source": "o"}}), which can't be resolved without this map.
    """
    aliases: dict[str, str] = {}
    entries = node.get("From")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                alias = entry.get("Name")
                entity = entry.get("Entity")
                if isinstance(alias, str) and isinstance(entity, str):
                    aliases[alias] = entity
    return aliases


def _expression_entity(expression, aliases: dict) -> str:
    """Table referenced by a PBIR Expression, resolving the 'From' aliases."""
    if not isinstance(expression, dict):
        return ""
    source_ref = expression.get("SourceRef")
    if not isinstance(source_ref, dict):
        return ""
    entity = source_ref.get("Entity")
    if isinstance(entity, str):
        return entity
    source = source_ref.get("Source")
    if isinstance(source, str):
        return aliases.get(source, "")
    return ""


def _text(value) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def collect_pbir_references(data) -> dict:
    """
    Index of the references contained in ONE PBIR file, in a single pass.

    The document is walked ONCE and everything needed to answer about any
    field is extracted; matching against the targets becomes a set lookup.
    The walk is ITERATIVE: the recursive one raised RecursionError on deeply
    nested PBIR files, and the exception escaped the caller's per-file
    handling, failing the whole workspace scan.

    {"measures": {name}, "columns": {(table, column)},
     "levels": {(table, hierarchy, level)}, "texts": {value},
     "last_segments": {segment after the last dot}, "loose_columns": {name},
     "loose_measures": {name}}

    Hierarchy levels are kept apart from columns: a level has its own name,
    which is not necessarily the name of the column behind it (level 'Year'
    on column 'Calendar Year'). Only the model can tell which column it is,
    see `build_level_map`.
    """
    measures: set[str] = set()
    columns: set[tuple[str, str]] = set()
    levels: set[tuple[str, str, str]] = set()
    texts: set[str] = set()

    stack: list[tuple] = [(data, {})]
    while stack:
        node, aliases = stack.pop()

        if isinstance(node, list):
            for item in node:
                stack.append((item, aliases))
            continue
        if not isinstance(node, dict):
            continue

        new_aliases = _from_alias_map(node)
        local_aliases = {**aliases, **new_aliases} if new_aliases else aliases

        measure = node.get("Measure")
        if isinstance(measure, dict):
            name = _text(measure.get("Property"))
            if name:
                measures.add(name)

        column = node.get("Column")
        if isinstance(column, dict):
            name = _text(column.get("Property"))
            if name:
                columns.add((_text(_expression_entity(column.get("Expression"), local_aliases)), name))

        # Auto date/time hierarchy: the column is the date the variation hangs on.
        variation = node.get("PropertyVariationSource")
        if isinstance(variation, dict):
            name = _text(variation.get("Property"))
            if name:
                columns.add((_text(_expression_entity(variation.get("Expression"), local_aliases)), name))

        level = node.get("HierarchyLevel")
        if isinstance(level, dict):
            name = _text(level.get("Level"))
            if name:
                expression = level.get("Expression")
                hierarchy = expression.get("Hierarchy") if isinstance(expression, dict) else None
                table, hierarchy_name = "", ""
                if isinstance(hierarchy, dict):
                    table = _text(_expression_entity(hierarchy.get("Expression"), local_aliases))
                    hierarchy_name = _text(hierarchy.get("Hierarchy"))
                levels.add((table, hierarchy_name, name))

        for key, value in node.items():
            if isinstance(value, str):
                if key.lower() in PBIR_TEXT_KEYS:
                    text = value.strip().lower()
                    if text:
                        texts.add(text)
                continue
            if isinstance(value, (dict, list)):
                stack.append((value, local_aliases))

    return {
        "measures": measures,
        "columns": columns,
        "levels": levels,
        "texts": texts,
        "last_segments": {t.rsplit(".", 1)[-1] for t in texts if "." in t},
        "loose_columns": set(),
        "loose_measures": set(),
    }


def dax_reference_index(expression: str) -> dict:
    """
    Same index as `collect_pbir_references`, for a DAX expression written in
    the report (report-level measures in 'reportExtensions.json').

    Without the model, a bracketed name is decided by its form, as DAX best
    practice writes it: an unqualified `[Name]` is a measure (or, inside an
    iterator, possibly a column: a probable column match), a qualified
    `Table[Name]` is a column (and only possibly a measure, since a measure
    can be qualified too: a probable measure match). Every bracketed name used
    to count as a CERTAIN measure, so `SUM(Sales[Amount])` made a measure
    named 'Amount' look used.
    """
    measures: set[str] = set()
    columns: set[tuple[str, str]] = set()
    loose_columns: set[str] = set()
    loose_measures: set[str] = set()
    for ref in dax_lexer.analyze(expression).references:
        name = ref.name.strip().lower()
        table = ref.table
        if table and not ref.table_quoted and dax_lexer.is_keyword(table):
            table = ""
        if table:
            columns.add((table.strip().lower(), name))
            loose_measures.add(name)
        else:
            measures.add(name)
            loose_columns.add(name)
    return {"measures": measures, "columns": columns, "levels": set(), "texts": set(),
            "last_segments": set(), "loose_columns": loose_columns,
            "loose_measures": loose_measures}


def build_level_map(model: dict) -> dict[tuple[str, str, str], str]:
    """{(table, hierarchy, level): column}, all lowercase, from a loaded model."""
    level_map: dict[tuple[str, str, str], str] = {}
    for table_name, meta in model.items():
        for hierarchy, levels in meta.get("hierarchies", {}).items():
            for level, column in levels.items():
                level_map[(table_name.lower(), hierarchy.lower(), level.lower())] = column.lower()
    return level_map


def match_references(index: dict, target: dict, level_map: dict | None = None) -> str:
    """
    Outcome of matching an index from `collect_pbir_references` against a
    target: MATCH_BINDING, MATCH_TEXT or MATCH_NONE.

    `level_map` (from `build_level_map`, for the model the report is bound
    to) turns a hierarchy level into its real column: without it a level
    whose name equals the column name is only a probable match, because
    levels can be renamed.
    """
    kind = (target.get("kind") or "measure").strip().lower()
    name = _text(target.get("name"))
    table = _text(target.get("table"))
    if not name:
        return MATCH_NONE

    probable = False
    if kind == "measure":
        if name in index["measures"]:
            return MATCH_BINDING
        if name in index.get("loose_measures", ()):
            probable = True
        exact_texts = {name}
    else:
        for found_table, column in index["columns"]:
            if column != name:
                continue
            # Target without a table: any column with that name counts.
            if not table or found_table == table:
                return MATCH_BINDING
        for level_table, hierarchy, level in index.get("levels", ()):
            if table and level_table != table:
                continue
            column = (level_map or {}).get((level_table, hierarchy, level))
            if column is not None:
                if column == name:
                    return MATCH_BINDING
            elif level == name:
                probable = True
        if name in index.get("loose_columns", ()):
            probable = True
        exact_texts = {f"{table}.{name}"} if table else set()

    if probable or exact_texts & index["texts"]:
        return MATCH_TEXT

    # '.name' suffix: for measures it is the 'Table.Measure' form of
    # queryRefs; for a column searched without a table it is the only
    # possible textual hook. For qualified columns the exact comparison above
    # has already decided, so it is not widened.
    search_suffix = kind == "measure" or not table
    if search_suffix:
        if "." in name:
            suffix = f".{name}"
            if any(t.endswith(suffix) for t in index["texts"]):
                return MATCH_TEXT
        elif name in index["last_segments"]:
            return MATCH_TEXT

    return MATCH_NONE


def find_field_references_in_json(data, target: dict) -> str:
    """
    Searches a PBIR structure for a reference to a model field.

    `target` is {"kind": "measure", "name": ...} or
    {"kind": "column", "table": ..., "name": ...}.

    Returns HOW the field was recognized, not a plain boolean:

      MATCH_BINDING  the report really binds that field. Certain match.
      MATCH_TEXT     the name appears only in a text field (`queryRef`,
                     `Restatement`, `NativeReferenceName`, `displayName`), or
                     as a hierarchy level that can't be traced to its column.
                     That is how dynamic titles and conditional formatting
                     are found, but it is also how a false positive is born:
                     a visual titled 'Margin' triggers the 'Margin' measure.
      MATCH_NONE     no match.

    A binding always wins over text: if a file contains even one real
    reference, the outcome is certain.
    """
    return match_references(collect_pbir_references(data), target)


def find_measure_references_in_json(data, measure_name: str) -> bool:
    """Compatibility helper: reference to a measure, via `find_field_references_in_json`."""
    return bool(find_field_references_in_json(data, {"kind": "measure", "name": measure_name}))
