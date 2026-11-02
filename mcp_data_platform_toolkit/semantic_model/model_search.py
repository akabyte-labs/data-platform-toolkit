"""
Search across the whole semantic model: object names, their DAX or M, and
their '///' descriptions, one line per match.

Reading every table with `read_table_definition` to find where a column is
mentioned, or which measure is called "margin", costs the whole TMDL of the
model in tokens. This answers the same question with the name of each
matching object and the line that matched.

What "expression" covers, per object:
  - measure: its DAX, and its dynamic format string (formatStringDefinition);
  - column: the DAX of a calculated column, the sourceColumn of a data column;
  - calculation item: its DAX and its formatStringDefinition;
  - partition: its source (M, or the DAX of a calculated table);
  - shared expression / parameter: its M;
  - function: its DAX;
  - RLS filter: the DAX filter of a role on a table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.model_loader import find_table, load_definition

SEARCH_FIELDS = ("name", "expression", "description")

# Object types accepted by `object_types`, mapped to the label shown.
OBJECT_TYPES = {
    "table": "table",
    "column": "column",
    "measure": "measure",
    "hierarchy": "hierarchy",
    "calculationitem": "calculation item",
    "calcitem": "calculation item",
    "partition": "partition",
    "expression": "expression",
    "parameter": "expression",
    "function": "function",
    "role": "rls filter",
    "rls": "rls filter",
}

# Output order: the objects asked about most first.
_KIND_ORDER = ["measure", "column", "table", "hierarchy", "calculation item", "partition",
               "expression", "function", "rls filter"]

_SNIPPET_CHARS = 120


@dataclass
class _Candidate:
    kind: str
    label: str
    table: str
    name: str
    hidden: bool = False
    description: str = ""
    expressions: list[tuple[str, str]] = field(default_factory=list)    # (what, text)


def _description(lines: list[str], block) -> str:
    return "\n".join(line.strip()[3:].strip() for line in tmdl.get_doc_comments(lines, block))


def _ref(table: str, name: str) -> str:
    quoted = f"'{table}'" if any(c in table for c in " .'=:") else table
    return f"{quoted}[{name}]"


def _candidates(project_path: str) -> list[_Candidate]:
    definition = load_definition(project_path)
    found: list[_Candidate] = []

    for table, meta in definition["tables"].items():
        docs = [_description(p["lines"], p["table_block"]) for p in meta["parts"]]
        found.append(_Candidate(
            "table", table, table, table,
            hidden=any(tmdl.is_hidden(p["lines"], p["table_block"]) for p in meta["parts"]),
            description="\n".join(d for d in docs if d)))

        for obj in meta["column_objects"]:
            if obj.block.inline_expression is not None:
                texts = [("DAX", obj.expression())]
            else:
                source = tmdl.get_property(obj.lines, obj.block, "sourceColumn")
                texts = [("sourceColumn", tmdl.normalize_text_value(source.value))] if source else []
            found.append(_Candidate("column", _ref(table, obj.name), table, obj.name,
                                    tmdl.is_hidden(obj.lines, obj.block),
                                    _description(obj.lines, obj.block), texts))

        for kind, objects in (("measure", meta["measures"]), ("calculation item", meta["calc_items"])):
            for obj in objects:
                texts = [("DAX", obj.expression())]
                format_string = tmdl.get_expression_property(obj.lines, obj.block, "formatStringDefinition")
                if format_string:
                    texts.append(("format string", format_string))
                found.append(_Candidate(kind, _ref(table, obj.name), table, obj.name,
                                        tmdl.is_hidden(obj.lines, obj.block),
                                        _description(obj.lines, obj.block), texts))

        for part in meta["parts"]:
            for block in tmdl.child_blocks(part["lines"], part["table_block"], "hierarchy"):
                found.append(_Candidate("hierarchy", _ref(table, block.name), table, block.name,
                                        tmdl.is_hidden(part["lines"], block),
                                        _description(part["lines"], block)))

        for obj in meta["partitions"]:
            kind = (obj.block.inline_expression or "").strip().lower()
            source = tmdl.get_expression_property(obj.lines, obj.block, "source") or ""
            what = "DAX" if kind == "calculated" else "M" if kind in ("m", "") else kind or "source"
            found.append(_Candidate("partition", f"{table} / partition '{obj.name}'", table, obj.name,
                                    description=_description(obj.lines, obj.block),
                                    expressions=[(what, source)] if source else []))

    for kind, objects, what in (("expression", definition["expressions"], "M"),
                                ("function", definition["functions"], "DAX")):
        for obj in objects.values():
            found.append(_Candidate(kind, obj.name, "", obj.name,
                                    description=_description(obj.lines, obj.block),
                                    expressions=[(what, obj.expression())]))

    tables = {name.lower(): name for name in definition["tables"]}
    for role in definition["roles"].values():
        for tp in tmdl.child_blocks(role.lines, role.block, "tablePermission"):
            expression = tmdl.get_expression(role.lines, tp).strip()
            if not expression:
                continue                        # OLS only: no DAX to search
            table = tables.get(tp.name.strip().lower(), tp.name.strip())
            found.append(_Candidate("rls filter", f"role '{role.name}' on {table}", table, role.name,
                                    description=_description(role.lines, role.block),
                                    expressions=[("DAX", expression)]))
    return found


def _matcher(query: str, regex: bool):
    text = str(query or "")
    if not text.strip():
        raise ValueError("The search text cannot be empty.")
    if regex:
        try:
            pattern = re.compile(text, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression '{text}': {exc}.") from exc
    else:
        pattern = re.compile(re.escape(text), re.IGNORECASE)
    return pattern.search


def _snippet(line: str, match) -> str:
    text = line.strip()
    if len(text) <= _SNIPPET_CHARS:
        return text
    offset = len(line) - len(line.lstrip())
    start = max(0, match.start() - offset - 40)
    piece = text[start:start + _SNIPPET_CHARS]
    return ("..." if start else "") + piece + ("..." if start + _SNIPPET_CHARS < len(text) else "")


def _first_line_match(search, text: str) -> tuple[int, str, int] | None:
    """(line number, snippet, number of matching lines) of a multi-line text."""
    first = None
    count = 0
    for number, line in enumerate(text.splitlines(), start=1):
        match = search(line)
        if match:
            count += 1
            if first is None:
                first = (number, _snippet(line, match))
    return None if first is None else (first[0], first[1], count)


def _match(candidate: _Candidate, search, fields: tuple[str, ...]) -> list[str]:
    """What matched, most telling first: ["name"], ["expression (DAX, line 2): ..."], ..."""
    hits: list[str] = []
    if "name" in fields and search(candidate.name):
        hits.append("name")
    if "expression" in fields:
        for what, text in candidate.expressions:
            found = _first_line_match(search, text)
            if found:
                number, snippet, count = found
                where = what if "\n" not in text.strip() else f"{what}, line {number}"
                more = f"  (+{count - 1} more line{'s' if count > 2 else ''})" if count > 1 else ""
                hits.append(f"{where}: {snippet}{more}")
    if "description" in fields and candidate.description:
        found = _first_line_match(search, candidate.description)
        if found:
            hits.append(f"description: {found[1]}")
    return hits


def _normalize_fields(search_in) -> tuple[str, ...]:
    if not search_in:
        return SEARCH_FIELDS
    values = [search_in] if isinstance(search_in, str) else list(search_in)
    fields = []
    for value in values:
        key = str(value or "").strip().lower()
        if key in ("names",):
            key = "name"
        elif key in ("expressions", "dax", "m", "code"):
            key = "expression"
        elif key in ("descriptions", "doc", "docs"):
            key = "description"
        if key not in SEARCH_FIELDS:
            raise ValueError(f"Invalid search_in value '{value}'. Allowed: {', '.join(SEARCH_FIELDS)}.")
        if key not in fields:
            fields.append(key)
    return tuple(fields)


def _normalize_types(object_types) -> set[str] | None:
    if not object_types:
        return None
    values = [object_types] if isinstance(object_types, str) else list(object_types)
    kinds = set()
    for value in values:
        key = re.sub(r"[\s_-]", "", str(value or "")).lower()
        key = {"hierarchies": "hierarchy", "rlsfilter": "rls", "rlsfilters": "rls",
               "roles": "role"}.get(key, key)
        if key not in OBJECT_TYPES and key.endswith("s") and key[:-1] in OBJECT_TYPES:
            key = key[:-1]                  # 'measures', 'columns', 'calculationItems'
        if key not in OBJECT_TYPES:
            allowed = ", ".join(sorted(set(OBJECT_TYPES) - {"calcitem", "parameter", "rls"}))
            raise ValueError(f"Invalid object type '{value}'. Allowed: {allowed}.")
        kinds.add(OBJECT_TYPES[key])
    return kinds


def search_model(project_path: str, query: str, search_in=None, object_types=None,
                 table_name: str = "", regex: bool = False) -> dict:
    """
    {"query", "fields", "matches": [{kind, label, hidden, hits}], "counts": {kind: n}}
    sorted by kind (measures first), then table, then name.
    """
    search = _matcher(query, regex)
    fields = _normalize_fields(search_in)
    kinds = _normalize_types(object_types)

    table_filter = ""
    if (table_name or "").strip():
        meta = find_table(load_definition(project_path)["tables"], table_name)
        if meta is None:
            raise FileNotFoundError(f"Table '{table_name}' not found in the semantic model.")
        table_filter = meta["name"].lower()

    matches = []
    for candidate in _candidates(project_path):
        if kinds is not None and candidate.kind not in kinds:
            continue
        if table_filter and candidate.table.lower() != table_filter:
            continue
        hits = _match(candidate, search, fields)
        if hits:
            matches.append({"kind": candidate.kind, "label": candidate.label,
                            "table": candidate.table, "name": candidate.name,
                            "hidden": candidate.hidden, "hits": hits})

    matches.sort(key=lambda m: (_KIND_ORDER.index(m["kind"]), m["table"].lower(), m["name"].lower()))
    counts: dict[str, int] = {}
    for item in matches:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return {"query": query, "fields": fields, "matches": matches, "counts": counts}


def format_search(result: dict, table_name: str = "") -> str:
    scope = ", ".join(result["fields"]) + (f", table '{table_name}'" if table_name else "")
    matches = result["matches"]
    if not matches:
        return f"No match for '{result['query']}' in {scope}."

    plural = {"measure": "measures", "column": "columns", "table": "tables",
              "hierarchy": "hierarchies", "calculation item": "calculation items",
              "partition": "partitions", "expression": "expressions", "function": "functions",
              "rls filter": "rls filters"}
    counts = ", ".join(f"{plural[k] if n != 1 else k} {n}"
                       for k, n in sorted(result["counts"].items(), key=lambda i: _KIND_ORDER.index(i[0])))
    lines = [f"=== SEARCH '{result['query']}' in {scope}: {len(matches)} "
             f"match{'es' if len(matches) != 1 else ''} ({counts}) ==="]
    for item in matches:
        label = item["label"] + (" [hidden]" if item["hidden"] else "")
        lines.append(f"{item['kind']}: {label}  -- " + " | ".join(item["hits"]))
    return "\n".join(lines)
