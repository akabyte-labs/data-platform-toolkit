"""
Perspectives of the semantic model: which tables, columns, measures and
hierarchies each one shows, and which of its members no longer exist.

A perspective names its members by name. A measure renamed or deleted
outside Power BI Desktop (by hand, by a script, by a merge) leaves the
perspective pointing to an object that isn't there: those members are
reported, never silently dropped.

TMDL shape (perspectives/<name>.tmdl):

    perspective SalesView
        perspectiveTable Sales
            perspectiveMeasure 'Sales Amount'
            perspectiveColumn Quantity
        perspectiveTable Customer
            includeAll
"""

from __future__ import annotations

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.model_loader import find_table, load_definition
from mcp_data_platform_toolkit.semantic_model.tmdl_parser import ElementNotFoundError

_MEMBER_KINDS = (("perspectiveMeasure", "measure", "measures"),
                 ("perspectiveColumn", "column", "columns"),
                 ("perspectiveHierarchy", "hierarchy", "hierarchies"))


def _declared(meta: dict, kind: str) -> dict[str, str]:
    """{lowercase name: declared name} of a table's members of one kind."""
    if kind == "measure":
        names = [m.name for m in meta["measures"]]
    elif kind == "column":
        names = meta["columns"]
    else:
        names = list(meta["hierarchies"])
    return {n.strip().lower(): n for n in names}


def perspective_state(project_path: str, perspective_name: str = "") -> list[dict]:
    """
    [{"name", "file", "tables": [{"table", "include_all", "members": {kind: [names]}}],
      "missing": ["Table[Name] (kind)" | "Table (table)"]}] sorted by name.
    """
    definition = load_definition(project_path)
    tables = definition["tables"]
    objects = sorted(definition["perspectives"].values(), key=lambda o: o.name.lower())
    if perspective_name:
        target = perspective_name.strip().lower()
        objects = [o for o in objects if o.name.strip().lower() == target]
        if not objects:
            names = ", ".join(sorted(definition["perspectives"])) or "none"
            raise ElementNotFoundError(
                f"Perspective '{perspective_name}' not found in the model (perspectives: {names}).")

    state = []
    for obj in objects:
        entry = {"name": obj.name, "file": obj.file_path, "tables": [], "missing": []}
        for pt in tmdl.child_blocks(obj.lines, obj.block, "perspectiveTable"):
            meta = find_table(tables, pt.name)
            table = meta["name"] if meta else pt.name.strip()
            if meta is None:
                entry["missing"].append(f"{table} (table)")
            item = {"table": table,
                    "include_all": tmdl.get_bool_property(obj.lines, pt, "includeAll"),
                    "members": {}}
            for keyword, kind, _plural in _MEMBER_KINDS:
                declared = _declared(meta, kind) if meta else {}
                for member in tmdl.child_blocks(obj.lines, pt, keyword):
                    name = declared.get(member.name.strip().lower())
                    if name is None:
                        if meta is not None:
                            entry["missing"].append(f"{table}[{member.name.strip()}] ({kind})")
                        name = member.name.strip()
                    item["members"].setdefault(kind, []).append(name)
            entry["tables"].append(item)
        state.append(entry)
    return state


def format_perspectives(state: list[dict], all_tables: list[str], filtered: bool) -> str:
    if not state:
        return "No perspectives defined in the model."

    lines = [f"=== PERSPECTIVES ({len(state)}) ==="]
    in_some: set[str] = set()
    for entry in state:
        totals = []
        for _keyword, kind, plural in _MEMBER_KINDS:
            count = sum(len(t["members"].get(kind, [])) for t in entry["tables"])
            if count:
                totals.append(f"{count} {kind if count == 1 else plural}")
        whole = sum(1 for t in entry["tables"] if t["include_all"])
        if whole:
            totals.append(f"{whole} whole table{'s' if whole != 1 else ''}")
        tables = len(entry["tables"])
        lines.append(f"\nPerspective: {entry['name']}  ({tables} table{'s' if tables != 1 else ''}"
                     + (f"; {', '.join(totals)}" if totals else "") + ")")
        for item in entry["tables"]:
            in_some.add(item["table"].lower())
            if item["include_all"]:
                lines.append(f"  {item['table']}: the whole table (includeAll)")
                continue
            parts = [f"{plural}: {', '.join(item['members'][kind])}"
                     for _keyword, kind, plural in _MEMBER_KINDS if item["members"].get(kind)]
            lines.append(f"  {item['table']}: " + (" | ".join(parts) if parts else "the table only, no members"))
        if entry["missing"]:
            lines.append("  [!] not in the model (references that don't resolve): "
                         + ", ".join(entry["missing"]))

    if not filtered:
        outside = [t for t in sorted(all_tables, key=str.lower) if t.lower() not in in_some]
        if outside:
            lines.append("\nTables in no perspective: " + ", ".join(outside))
    return "\n".join(lines)
