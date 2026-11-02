"""
How much of a semantic model carries a '///' description, for the whole model
or for one table: measures, columns and the rest (tables, hierarchies,
calculation items), the visible ones apart from the hidden ones.

The two are told apart because they mean different things. What the user sees
in Power BI (visible elements) is what a Data Dictionary has to explain; the
columns of a hidden fact table are read by the measures and never by a person,
so leaving them undocumented is a choice and counting them with the rest would
make a well-documented model look empty. 'Hidden' is what `get_table_summary`
calls hidden: the element's own flag, or its table's.

The counts of a table are the ones `get_table_summary` gives for it: a test
keeps the two equal.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.model_loader import find_table, get_table_file_path, load_definition
from mcp_data_platform_toolkit.semantic_model.project_paths import find_semantic_model_folder

# What the response groups the elements into.
_GROUPS = (("measure", "Measures"), ("column", "Columns"),
           ("other", "Tables, hierarchies and calculation items"))
_KIND_GROUP = {"measure": "measure", "column": "column", "table": "other", "hierarchy": "other",
               "calculation item": "other"}

# How a kind is counted in the ranking of tables: (one, several).
_KIND_WORDS = {"measure": ("measure", "measures"), "column": ("column", "columns"),
               "table": ("table description", "table descriptions"), "hierarchy": ("hierarchy", "hierarchies"),
               "calculation item": ("calculation item", "calculation items")}

TABLES_SHOWN = 30
NAMES_SHOWN = 80


def _documented(lines: list[str], block) -> bool:
    return any(line.strip()[3:].strip() for line in tmdl.get_doc_comments(lines, block))


def elements(meta: dict) -> Iterator[tuple[str, str, bool, bool]]:
    """(kind, name, hidden, documented) of every element of a table."""
    hidden_table = any(tmdl.is_hidden(p["lines"], p["table_block"]) for p in meta["parts"])
    yield ("table", meta["name"], hidden_table,
           any(_documented(p["lines"], p["table_block"]) for p in meta["parts"]))
    for obj in meta["column_objects"]:
        yield ("column", obj.name, hidden_table or tmdl.is_hidden(obj.lines, obj.block),
               _documented(obj.lines, obj.block))
    for obj in meta["measures"]:
        yield ("measure", obj.name, hidden_table or tmdl.is_hidden(obj.lines, obj.block),
               _documented(obj.lines, obj.block))
    for obj in meta["calc_items"]:
        yield "calculation item", obj.name, hidden_table, _documented(obj.lines, obj.block)
    for part in meta["parts"]:
        for block in tmdl.child_blocks(part["lines"], part["table_block"], "hierarchy"):
            yield ("hierarchy", block.name, hidden_table or tmdl.is_hidden(part["lines"], block),
                   _documented(part["lines"], block))


def _share(documented: int, total: int) -> str:
    return f"{documented} of {total} ({round(100 * documented / total)}%)" if total else "none"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def documentation_coverage(project_path: str, table_name: str = "") -> str:
    definition = load_definition(project_path)
    tables = definition["tables"]
    if table_name and table_name.strip():
        meta = find_table(tables, table_name)
        if meta is None:
            get_table_file_path(project_path, table_name)      # raises the path / not-found error
            raise FileNotFoundError(f"Table '{table_name}' not found in the semantic model.")
        tables = {meta["name"]: meta}

    # (group, hidden) -> [documented, total]
    totals: dict[tuple[str, bool], list[int]] = {(g, h): [0, 0] for g, _t in _GROUPS for h in (False, True)}
    per_table: dict[str, dict] = {}
    for name, meta in tables.items():
        entry = per_table[name] = {"visible": 0, "documented": 0, "missing": {}, "names": [], "hidden": False}
        for kind, element, hidden, documented in elements(meta):
            counts = totals[(_KIND_GROUP[kind], hidden)]
            counts[1] += 1
            counts[0] += documented
            if kind == "table":
                entry["hidden"] = hidden
            if hidden:
                continue
            entry["visible"] += 1
            entry["documented"] += documented
            if not documented:
                entry["missing"][kind] = entry["missing"].get(kind, 0) + 1
                entry["names"].append(f"{kind}: {element}")

    model = os.path.basename(find_semantic_model_folder(project_path).rstrip("/\\"))
    scope = f"table {next(iter(tables))}" if table_name and table_name.strip() else f"{_plural(len(tables), 'table')}"
    lines = [f"=== DOCUMENTATION COVERAGE: {model} ({scope}) ===",
             "Elements with a '///' description. Visible = what a person sees in Power BI; hidden = hidden by "
             "its own flag or by its table (the columns of a hidden fact table are read by measures, not people).",
             "", f"{'':<45}{'visible':<26}hidden"]
    for group, title in _GROUPS:
        visible, hidden = totals[(group, False)], totals[(group, True)]
        lines.append(f"{title:<45}{_share(*visible):<26}{_share(*hidden)}")
    shown = [totals[(g, False)] for g, _t in _GROUPS]
    lines.append(f"{'Visible, all together':<45}{_share(sum(c[0] for c in shown), sum(c[1] for c in shown))}")

    if definition.get("errors"):
        lines.append(f"[!] {_plural(len(definition['errors']), 'file')} could not be read, so their elements "
                     f"are not counted: " + "; ".join(definition["errors"][:5]))

    if table_name and table_name.strip():
        name, entry = next(iter(per_table.items()))
        lines.append("")
        if entry["hidden"]:
            lines.append(f"'{name}' is a hidden table: all its elements count as hidden.")
        elif not entry["names"]:
            lines.append(f"Every visible element of '{name}' is documented.")
        else:
            lines.append(f"Visible elements of '{name}' without a description "
                         f"({len(entry['names'])} of {entry['visible']}):")
            lines.extend(f"  {text}" for text in entry["names"][:NAMES_SHOWN])
            if len(entry["names"]) > NAMES_SHOWN:
                lines.append(f"  ... and {len(entry['names']) - NAMES_SHOWN} more")
        return "\n".join(lines)

    incomplete = sorted(((n, e) for n, e in per_table.items() if e["visible"] and e["documented"] < e["visible"]),
                        key=lambda item: (-(item[1]["visible"] - item[1]["documented"]), item[0].lower()))
    lines.append("")
    if not incomplete:
        lines.append("Every visible element of the model is documented.")
        return "\n".join(lines)
    lines.append(f"Tables with visible elements still undocumented ({len(incomplete)}), most missing first:")
    for name, entry in incomplete[:TABLES_SHOWN]:
        missing = entry["visible"] - entry["documented"]
        kinds = ", ".join(f"{count} {_KIND_WORDS[kind][count != 1]}"
                          for kind, count in sorted(entry["missing"].items(), key=lambda i: (-i[1], i[0])))
        lines.append(f"  {name}: {missing} of {entry['visible']} visible ({kinds})")
    if len(incomplete) > TABLES_SHOWN:
        lines.append(f"  ... and {len(incomplete) - TABLES_SHOWN} more tables")
    lines.append("A table with 'table_name' lists the names of its undocumented visible elements.")
    return "\n".join(lines)

