"""
A table of the semantic model in a few lines: its columns with their type
and flags, its measures with folder and format, hierarchies, partitions with
the source they read, relationships and how much of it is documented. No
DAX and no M: `read_table_definition` returns those, at many times the size.
"""

from __future__ import annotations

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.model_loader import find_table, get_table_file_path, load_definition
from mcp_data_platform_toolkit.semantic_model.power_query import model_sources
from mcp_data_platform_toolkit.semantic_model.relationships import cardinality_symbol, parse_relationships

_DESCRIPTION_CHARS = 100


def _text(lines: list[str], block, name: str) -> str:
    child = tmdl.get_property(lines, block, name)
    return tmdl.normalize_text_value(child.value) if child and child.value else ""


def _description(lines: list[str], block) -> str:
    return " ".join(line.strip()[3:].strip() for line in tmdl.get_doc_comments(lines, block)).strip()


def _short(text: str) -> str:
    return text if len(text) <= _DESCRIPTION_CHARS else text[:_DESCRIPTION_CHARS].rstrip() + "..."


def _flags(parts: list[str], description: str, include_descriptions: bool) -> str:
    text = " | ".join(p for p in parts if p)
    if include_descriptions and description:
        text += f'  -- "{_short(description)}"'
    return text


def _sources(project_path: str, table: str) -> dict[str, str]:
    """{partition: what it reads}, from the Power Query analysis; {} if it can't be read."""
    try:
        sources = [s for s in model_sources(project_path) if s.table == table]
    except Exception:  # noqa: BLE001 - the summary stands without the sources
        return {}
    described: dict[str, str] = {}
    for source in sources:
        names = []
        for origin in source.objects():
            if origin.kind == "db":
                names.append(f"{origin.schema}.{origin.name}" if origin.schema else origin.name)
            elif origin.kind == "native":
                names.append("native query")
            else:
                names.append(" ".join(p for p in (origin.connector, origin.name) if p) or "external")
        if names:
            described[source.partition] = ", ".join(dict.fromkeys(names))
    return described


def table_summary(project_path: str, table_name: str, include_descriptions: bool = False) -> str:
    definition = load_definition(project_path)
    meta = find_table(definition["tables"], table_name)
    if meta is None:
        get_table_file_path(project_path, table_name)      # raises the path / not-found error
        raise FileNotFoundError(f"Table '{table_name}' not found in the semantic model.")
    name = meta["name"]

    documented = visible = 0

    def count(description: str, hidden: bool) -> None:
        nonlocal documented, visible
        if not hidden:
            visible += 1
            documented += bool(description)

    # Table
    hidden_table = any(tmdl.is_hidden(p["lines"], p["table_block"]) for p in meta["parts"])
    table_doc = " ".join(d for d in (_description(p["lines"], p["table_block"]) for p in meta["parts"]) if d)
    count(table_doc, hidden_table)

    relationships = [r for r in parse_relationships(project_path)
                     if name.lower() in (r["from_table"].lower(), r["to_table"].lower())]
    def on_many_side(r: dict) -> bool:
        cardinalities = (r["from_cardinality"], r["to_cardinality"])
        side = {("many", "one"): r["from_table"], ("one", "many"): r["to_table"]}.get(cardinalities, "")
        return side.lower() == name.lower()

    many_side = sum(1 for r in relationships if on_many_side(r))

    kinds = []
    if meta["is_calc_group"]:
        kinds.append("calculation group")
    partition_kinds = {(p.block.inline_expression or "").strip().lower() for p in meta["partitions"]}
    if "calculated" in partition_kinds:
        kinds.append("calculated table")
    if many_side:
        kinds.append(f"fact table (many side of {many_side} relationship{'s' if many_side != 1 else ''})")
    category = _text(meta["lines"], meta["table_block"], "dataCategory")
    if category:
        kinds.append(f"dataCategory: {category}")
    if hidden_table:
        kinds.append("hidden")

    # Columns
    column_lines = []
    for obj in meta["column_objects"]:
        lines, block = obj.lines, obj.block
        hidden = tmdl.is_hidden(lines, block)
        description = _description(lines, block)
        count(description, hidden or hidden_table)
        sort_by = _text(lines, block, "sortByColumn")
        details = _flags([
            "calculated" if block.inline_expression is not None else "",
            _text(lines, block, "dataType"),
            "hidden" if hidden else "",
            "key" if tmdl.get_bool_property(lines, block, "isKey") else "",
            f"folder: {_text(lines, block, 'displayFolder')}" if _text(lines, block, "displayFolder") else "",
            f"format: {_text(lines, block, 'formatString')}" if _text(lines, block, "formatString") else "",
            f"sortBy: {sort_by.strip(chr(39))}" if sort_by else "",
        ], description, include_descriptions)
        column_lines.append(f"  {obj.name}" + (f": {details}" if details else ""))

    # Measures
    measure_lines = []
    for obj in meta["measures"]:
        lines, block = obj.lines, obj.block
        hidden = tmdl.is_hidden(lines, block)
        description = _description(lines, block)
        count(description, hidden or hidden_table)
        format_string = _text(lines, block, "formatString")
        if tmdl.get_expression_property(lines, block, "formatStringDefinition"):
            format_string = "dynamic (formatStringDefinition)"
        measure_lines.append(f"  {obj.name}: " + _flags([
            "hidden" if hidden else "",
            f"folder: {_text(lines, block, 'displayFolder')}" if _text(lines, block, "displayFolder") else "",
            f"format: {format_string}" if format_string else "no format string",
            "KPI" if tmdl.child_blocks(lines, block, "kpi") else "",
        ], description, include_descriptions))

    # Calculation items
    item_lines = []
    for obj in meta["calc_items"]:
        description = _description(obj.lines, obj.block)
        count(description, hidden_table)
        ordinal = _text(obj.lines, obj.block, "ordinal")
        dynamic = tmdl.get_expression_property(obj.lines, obj.block, "formatStringDefinition")
        details = _flags([f"ordinal: {ordinal}" if ordinal else "",
                          "formatStringDefinition" if dynamic else ""], description, include_descriptions)
        item_lines.append(f"  {obj.name}" + (f": {details}" if details else ""))

    # Hierarchies
    hierarchy_lines = []
    for part in meta["parts"]:
        for block in tmdl.child_blocks(part["lines"], part["table_block"], "hierarchy"):
            hidden = tmdl.is_hidden(part["lines"], block)
            description = _description(part["lines"], block)
            count(description, hidden or hidden_table)
            levels = meta["hierarchies"].get(block.name, {})
            text = f"  {block.name}: " + " > ".join(levels.values() or ["(no levels)"])
            if hidden:
                text += " | hidden"
            if include_descriptions and description:
                text += f'  -- "{_short(description)}"'
            hierarchy_lines.append(text)

    # Partitions
    sources = _sources(project_path, name) if meta["partitions"] else {}
    partition_lines = []
    for obj in meta["partitions"]:
        kind = (obj.block.inline_expression or "").strip() or "m"
        mode = _text(obj.lines, obj.block, "mode") or "import"
        text = f"  {obj.name}: {kind}, {mode}"
        if obj.name in sources:
            text += f" | reads: {sources[obj.name]}"
        partition_lines.append(text)

    # Relationships
    relationship_lines = [
        f"  {r['from_table']}[{r['from_column']}] {cardinality_symbol(r)} {r['to_table']}[{r['to_column']}]"
        f" | {'active' if r['is_active'] else 'inactive'} | {r['cross_filtering']}"
        for r in relationships
    ]

    def plural(n: int, word: str, words: str) -> str:
        return f"{n} {word if n == 1 else words}"

    counts = [plural(len(column_lines), "column", "columns"),
              plural(len(measure_lines), "measure", "measures")]
    if item_lines:
        counts.append(plural(len(item_lines), "calculation item", "calculation items"))
    counts += [plural(len(hierarchy_lines), "hierarchy", "hierarchies"),
               plural(len(relationship_lines), "relationship", "relationships")]
    out = [f"=== TABLE {name}: {', '.join(counts)} ==="]
    if kinds:
        out.append("Kind: " + ", ".join(kinds))
    if len(meta["parts"]) > 1:
        out.append(f"Declared in {len(meta['parts'])} files: "
                   + ", ".join(f"{p['file_stem']}.tmdl" for p in meta["parts"]))
    if table_doc:
        out.append(f"Description: {_short(table_doc) if not include_descriptions else table_doc}")
    out.append(f"Documented ('///'): {documented} of {visible} visible elements")
    for title, section in (("Partitions", partition_lines), ("Columns", column_lines),
                           ("Measures", measure_lines), ("Calculation items", item_lines),
                           ("Hierarchies", hierarchy_lines), ("Relationships", relationship_lines)):
        if section:
            out.append(f"\n{title} ({len(section)}):")
            out.extend(section)
    return "\n".join(out)
