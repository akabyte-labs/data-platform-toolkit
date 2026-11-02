"""
The semantic model loaded from its TMDL files, cached and invalidated by the
files' fingerprint, plus the lookup of the file that declares a table or one
of its elements.

The whole 'definition' folder is read, not only 'tables/': shared
expressions (Power Query), DAX functions, relationships, roles and
perspectives are part of the model, and the end-to-end lineage needs them.
'cultures/' is skipped: translations and linguistic metadata, often the
largest files of a model, say nothing about how the model is computed.
`translations` reads them on its own, only when translations are asked for.

TMDL "partial declarations" are supported: a table may be split across
several files, and a file may declare several tables. Every element keeps
the file, the lines and the block it was read from, so readers and writers
work on the file that really declares it.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.project_paths import (
    PathNotAllowedError,
    confined_path,
    find_definition_folder,
    find_tables_folder,
    simple_name,
)
from mcp_data_platform_toolkit.semantic_model.tmdl_io import file_fingerprint, read_tmdl_lines
from mcp_data_platform_toolkit.semantic_model.tmdl_parser import ElementNotFoundError, TmdlBlock

_cache_lock = threading.Lock()


@dataclass(eq=False)
class ModelObject:
    """An object of the model and where it is declared."""
    keyword: str
    name: str
    table: str              # owning table, "" for model-level objects
    file_path: str
    lines: list[str]
    block: TmdlBlock

    def expression(self) -> str:
        return tmdl.get_expression(self.lines, self.block)


# ---------------------------------------------------------------------------
# Files of the model
# ---------------------------------------------------------------------------

# Folders of 'definition' that are not read (see the module docstring).
SKIPPED_FOLDERS = {"cultures"}

# Files whose failure fails the load. A table that can't be read makes every
# answer about the model wrong, and relationships decide the fact-table
# protection, which must fail closed. Any other unreadable file is recorded
# in `errors` and the rest of the model stays usable.
_REQUIRED_FILES = {"relationships.tmdl", "model.tmdl"}


def _definition_files(definition: str) -> list[str]:
    """All the .tmdl files of the model: 'tables/' first, then the others, sorted."""
    tables, others = [], []
    for root, dirs, files in os.walk(definition):
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIPPED_FOLDERS)
        for name in sorted(files):
            if name.lower().endswith(".tmdl"):
                path = os.path.join(root, name)
                in_tables = os.path.basename(root).lower() == "tables" and \
                    os.path.dirname(root) == definition
                (tables if in_tables else others).append(path)
    return tables + others


def _is_required(definition: str, path: str) -> bool:
    relative = os.path.relpath(path, definition)
    parts = relative.replace("\\", "/").lower().split("/")
    return parts[0] == "tables" or (len(parts) == 1 and parts[0] in _REQUIRED_FILES)


# ---------------------------------------------------------------------------
# Table lookup (paths are never built from an unchecked name)
# ---------------------------------------------------------------------------

def get_table_file_path(project_path: str, table_name: str) -> str:
    """
    Path of a table's .tmdl file: by file name (case-insensitive), then by
    the name declared in the file (`table 'Ratio: A/B'`). For a table split
    across several files, the file of its main declaration.

    The second lookup exists because Power BI alters a table name that isn't
    a valid file name: a table called 'Ratio: A/B' was unreachable, since its
    name was rejected as a path before being looked up. That lookup never
    builds a path from the name, so it cannot weaken the confinement: a name
    that matches no declared table is still rejected as a path.
    """
    text = str(table_name or "").strip()
    try:
        name = simple_name(text, "The table name")
        path_error = None
    except PathNotAllowedError as exc:
        name, path_error = None, exc

    tables_dir = find_tables_folder(project_path)
    if name is not None:
        direct = os.path.join(tables_dir, f"{name}.tmdl")
        if os.path.exists(direct):
            return confined_path(tables_dir, direct, f"The table '{name}'")
        expected = f"{name.lower()}.tmdl"
        for file_name in os.listdir(tables_dir):
            if file_name.lower() == expected:
                return os.path.join(tables_dir, file_name)

    meta = find_table(load_semantic_model(project_path), text)
    if meta is not None:
        definition = find_definition_folder(project_path)
        return confined_path(definition, meta["file_path"], f"The table '{meta['name']}'")

    if path_error is not None:
        raise path_error
    raise FileNotFoundError(
        f"Table '{text}' not found in the semantic model ('{tables_dir}')."
    )


def read_table_lines(project_path: str, table_name: str) -> tuple[str, list[str]]:
    file_path = get_table_file_path(project_path, table_name)
    return file_path, read_tmdl_lines(file_path)


def find_table(model: dict, table_name: str) -> dict | None:
    """The table's entry in a loaded model, by declared name or file name (case-insensitive)."""
    target = str(table_name or "").strip().lower()
    for name, meta in model.items():
        if name.lower() == target:
            return meta
    for meta in model.values():
        if meta["file_stem"].lower() == target:
            return meta
    return None


def table_blocks_named(lines: list[str], table_name: str) -> list[TmdlBlock]:
    """Top-level 'table' blocks of a file declaring `table_name` (case-insensitive)."""
    target = str(table_name or "").strip().lower()
    return [b for b in tmdl.find_all_blocks(lines)
            if b.keyword == "table" and not b.indent and b.name.strip().lower() == target]


def find_in_table(lines: list[str], table_block: TmdlBlock, element_name: str,
                  element_types: list[str]) -> TmdlBlock:
    """
    An element declared INSIDE a table block, trying the types in order.

    Searching the whole file returned the first element with that name in ANY
    table of the file: with several tables in one file, a column of another
    table.
    """
    target = str(element_name or "").strip().lower()
    for element_type in element_types:
        keyword = tmdl.normalize_element_type(element_type)
        if keyword == "table":
            if table_block.name.strip().lower() == target:
                return table_block
            continue
        for block in tmdl.find_all_blocks(lines):
            if (table_block.start < block.start < table_block.end and block.keyword == keyword
                    and block.name.strip().lower() == target):
                return block
    raise ElementNotFoundError(
        f"No declaration found for element '{element_name}' of type "
        f"'{', '.join(element_types)}' in table '{table_block.name}'."
    )


def locate_element(project_path: str, table_name: str, element_name: str,
                   element_types: list[str]) -> tuple[str, object, list[str], TmdlBlock]:
    """
    (file_path, fingerprint, FRESH lines, block) of an element of a table, in
    whichever file declares it. The lines are read again from disk, not taken
    from the cache: they are the ones a write will be based on, and the
    fingerprint is taken BEFORE reading them, so a change landing in between
    makes the write refuse instead of overwriting it.
    """
    meta = find_table(load_semantic_model(project_path), table_name)
    if meta is None:
        get_table_file_path(project_path, table_name)   # raises the path / not-found error
        raise FileNotFoundError(f"Table '{table_name}' not found in the semantic model.")

    for part in meta["parts"]:
        fingerprint = file_fingerprint(part["file_path"])
        lines = read_tmdl_lines(part["file_path"])
        for table_block in table_blocks_named(lines, meta["name"]):
            try:
                block = find_in_table(lines, table_block, element_name, element_types)
            except ElementNotFoundError:
                continue
            return part["file_path"], fingerprint, lines, block
    raise ElementNotFoundError(
        f"No declaration found for element '{element_name}' of type "
        f"'{', '.join(element_types)}' in table '{meta['name']}'."
    )


# ---------------------------------------------------------------------------
# Loading (cached)
# ---------------------------------------------------------------------------

# The model used to be reloaded from scratch on every use: a single call could
# read and re-parse EVERY table file several times, and autodoc reloaded it
# twice FOR EACH documented element. The cache is invalidated by the
# fingerprint (mtime + size) of all the model's files: if anyone touches one,
# the whole model is reloaded.
_MODEL_CACHE_MAX = 4
_model_cache: dict[str, tuple[tuple, dict]] = {}


def clear_model_cache() -> None:
    """Invalidates the cache of loaded models (used by tests)."""
    with _cache_lock:
        _model_cache.clear()


def load_definition(project_path: str) -> dict:
    """
    The whole model, read once and indexed.

    {"tables":        {name: table entry, see `load_semantic_model`},
     "relationships": [ModelObject],
     "expressions":   {name: ModelObject}   shared Power Query expressions and parameters,
     "functions":     {name: ModelObject}   DAX user-defined functions,
     "roles":         {name: ModelObject},
     "perspectives":  {name: ModelObject},
     "errors":        ["file: problem"]     files that couldn't be read (not tables)}

    The result is SHARED between callers (cache): treat it as read-only.
    Anyone who needs to modify a file re-reads it (`locate_element`,
    `read_table_lines`).
    """
    definition = find_definition_folder(project_path)
    if not os.path.isdir(definition):
        raise FileNotFoundError(f"TMDL definition folder does not exist: '{definition}'.")
    files = _definition_files(definition)
    key = os.path.normcase(os.path.abspath(str(project_path or "")))
    fingerprint = tuple((path, file_fingerprint(path)) for path in files)

    with _cache_lock:
        entry = _model_cache.get(key)
        if entry is not None and entry[0] == fingerprint:
            return entry[1]

    loaded = _load_definition(definition, files)
    if not loaded["tables"]:
        find_tables_folder(project_path)        # the usual error when there are no tables at all

    with _cache_lock:
        _model_cache[key] = (fingerprint, loaded)
        while len(_model_cache) > _MODEL_CACHE_MAX:
            _model_cache.pop(next(iter(_model_cache)))
    return loaded


def load_semantic_model(project_path: str) -> dict:
    """
    The tables of the model: {table_name: entry}, where an entry is

      name, file_stem, file_path, lines, table_block   the MAIN declaration
                                                       (the file named after the
                                                       table in 'tables/', if any)
      parts        [{file_path, lines, table_block}]   every declaration of the table
      columns      [name], column_objects [ModelObject]
      hierarchies  {hierarchy: {level: column}}
      measures, calc_items, partitions  [ModelObject]
      is_calc_group

    Shared, read-only: see `load_definition`.
    """
    return load_definition(project_path)["tables"]


def _unquote_name(value: str | None) -> str:
    """`'Calendar Year'` -> `Calendar Year` (object reference in a TMDL property)."""
    text = tmdl.normalize_text_value(value)
    if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    return text


def _load_definition(definition: str, files: list[str]) -> dict:
    loaded: dict = {"tables": {}, "relationships": [], "expressions": {}, "functions": {},
                    "roles": {}, "perspectives": {}, "errors": []}
    tables_by_key: dict[str, dict] = {}

    for file_path in files:
        try:
            lines = read_tmdl_lines(file_path)
        except (OSError, tmdl.TmdlError) as exc:
            if _is_required(definition, file_path):
                raise
            loaded["errors"].append(f"{os.path.relpath(file_path, definition)}: {exc}")
            continue
        blocks = tmdl.find_all_blocks(lines)
        file_stem = os.path.splitext(os.path.basename(file_path))[0]
        if (_is_required(definition, file_path) and file_path.lower().endswith(".tmdl")
                and os.path.basename(os.path.dirname(file_path)).lower() == "tables"
                and not any(b.keyword == "table" and not b.indent for b in blocks)):
            # It used to be listed under its file name, with nothing in it.
            loaded["errors"].append(
                f"{os.path.relpath(file_path, definition)}: no 'table' declaration found")

        for block in blocks:
            if block.indent:
                continue                    # only top-level declarations here
            obj = ModelObject(block.keyword, block.name, "", file_path, lines, block)
            if block.keyword == "table":
                entry = tables_by_key.get(block.name.strip().lower())
                if entry is None:
                    entry = _new_table_entry(block.name.strip() or file_stem)
                    tables_by_key[block.name.strip().lower()] = entry
                _add_table_part(entry, file_path, file_stem, lines, blocks, block)
            elif block.keyword == "relationship":
                loaded["relationships"].append(obj)
            elif block.keyword == "expression":
                loaded["expressions"].setdefault(block.name, obj)
            elif block.keyword == "function":
                loaded["functions"].setdefault(block.name, obj)
            elif block.keyword == "role":
                loaded["roles"].setdefault(block.name, obj)
            elif block.keyword == "perspective":
                loaded["perspectives"].setdefault(block.name, obj)

    for entry in tables_by_key.values():
        _choose_main_part(entry, definition)
        loaded["tables"][entry["name"]] = entry
    return loaded


def _new_table_entry(name: str) -> dict:
    return {
        "name": name, "file_stem": "", "file_path": "", "lines": [], "table_block": None,
        "parts": [], "columns": [], "column_objects": [], "hierarchies": {},
        "measures": [], "calc_items": [], "partitions": [], "is_calc_group": False,
    }


def _add_table_part(entry: dict, file_path: str, file_stem: str, lines: list[str],
                    blocks: list[TmdlBlock], table_block: TmdlBlock) -> None:
    """Adds one declaration of a table: the blocks inside it, in this file."""
    entry["parts"].append({"file_path": file_path, "file_stem": file_stem,
                           "lines": lines, "table_block": table_block})
    name = entry["name"]
    inside = [b for b in blocks if table_block.start < b.start < table_block.end]

    for block in tmdl.child_blocks(lines, table_block, "column"):
        if block.name:
            entry["columns"].append(block.name)
            entry["column_objects"].append(ModelObject("column", block.name, name, file_path, lines, block))
    for hierarchy in tmdl.child_blocks(lines, table_block, "hierarchy"):
        levels: dict[str, str] = {}
        for level in tmdl.child_blocks(lines, hierarchy, "level"):
            column = tmdl.get_property(lines, level, "column")
            levels[level.name] = _unquote_name(column.value) if column and column.value else level.name
        entry["hierarchies"][hierarchy.name] = levels
    for block in tmdl.child_blocks(lines, table_block, "partition"):
        entry["partitions"].append(ModelObject("partition", block.name, name, file_path, lines, block))
    for block in inside:
        if block.keyword == "measure":
            entry["measures"].append(ModelObject("measure", block.name, name, file_path, lines, block))
        elif block.keyword == "calculationItem":
            entry["calc_items"].append(ModelObject("calculationItem", block.name, name, file_path, lines, block))
        elif block.keyword == "calculationGroup":
            entry["is_calc_group"] = True


def _choose_main_part(entry: dict, definition: str) -> None:
    """
    The main declaration of a table: the file in 'tables/' named after it (the
    one Power BI Desktop writes), else the first one in 'tables/', else the
    first one found. It is where new elements of the table are created.
    """
    tables_dir = os.path.normcase(os.path.join(definition, "tables"))

    def rank(part: dict) -> tuple:
        in_tables = os.path.normcase(os.path.dirname(part["file_path"])) == tables_dir
        named = part["file_stem"].lower() == entry["name"].lower()
        return (not (in_tables and named), not in_tables)

    entry["parts"].sort(key=rank)
    main = entry["parts"][0]
    entry.update({"file_stem": main["file_stem"], "file_path": main["file_path"],
                  "lines": main["lines"], "table_block": main["table_block"]})
