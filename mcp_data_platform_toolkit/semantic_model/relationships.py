"""
Model relationships: a single parser, used both by the `get_relationships`
tool and by fact-table classification.
"""

from __future__ import annotations

import os

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.model_loader import load_definition


def split_qualified_ref(value: str) -> tuple[str, str]:
    """'Sales'.'Customer Key' -> ('Sales', 'Customer Key'). Handles quotes."""
    text = (value or "").strip()
    if not text:
        return "", ""
    parts: list[str] = []
    buffer: list[str] = []
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if quoted and index + 1 < len(text) and text[index + 1] == "'":
                buffer.append("'")
                index += 2
                continue
            quoted = not quoted
            index += 1
            continue
        if char == "." and not quoted:
            parts.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    if len(parts) >= 2:
        return ".".join(parts[:-1]).strip(), parts[-1].strip()
    return "", parts[0].strip()


def parse_relationships(project_path: str) -> list[dict]:
    """
    Model relationships, from whichever file of the model declares them
    (usually 'relationships.tmdl', in older projects 'model.tmdl').

    The cardinality is read, not assumed: `fromCardinality` defaults to
    'many' and `toCardinality` to 'one' (the TOM defaults, omitted from the
    file in the common case), but a one-to-one or many-to-many relationship
    declares them. Assuming "from = many side" always printed `*--1` for
    every relationship and marked the from side of a 1:1 as a fact table.
    """
    relationships: list[dict] = []
    seen: set[str] = set()
    for obj in load_definition(project_path)["relationships"]:
        key = obj.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        props = {
            (child.name or "").lower(): child.value
            for child in tmdl.iter_children(obj.lines, obj.block)
            if child.kind == "property"
        }
        from_table, from_column = split_qualified_ref(tmdl.normalize_text_value(props.get("fromcolumn")))
        to_table, to_column = split_qualified_ref(tmdl.normalize_text_value(props.get("tocolumn")))
        is_active = True
        if "isactive" in props:
            raw = props["isactive"]
            is_active = True if raw is None else tmdl.normalize_text_value(raw).lower() != "false"
        relationships.append({
            "name": obj.name,
            "from_table": from_table,
            "from_column": from_column,
            "to_table": to_table,
            "to_column": to_column,
            "from_cardinality": _cardinality(props.get("fromcardinality"), "many"),
            "to_cardinality": _cardinality(props.get("tocardinality"), "one"),
            "is_active": is_active,
            "cross_filtering": tmdl.normalize_text_value(props.get("crossfilteringbehavior")) or "singleDirection",
            # Whether an RLS filter also flows back from the many side: off
            # unless the relationship says so, whatever its cross filtering.
            "security_filtering": tmdl.normalize_text_value(
                props.get("securityfilteringbehavior")) or "oneDirection",
            "source_file": os.path.basename(obj.file_path),
        })
    return relationships


def _cardinality(raw: str | None, default: str) -> str:
    value = tmdl.normalize_text_value(raw).strip().lower()
    return value if value in ("one", "many") else default


def cardinality_symbol(relationship: dict) -> str:
    """'*--1', '1--1', '*--*' ... from side first."""
    symbol = {"many": "*", "one": "1"}
    return f"{symbol[relationship['from_cardinality']]}--{symbol[relationship['to_cardinality']]}"


def is_fact_table(project_path: str, table_name: str) -> bool:
    """
    A table is considered a fact table if it sits on the many side of at
    least one one-to-many relationship. (Checking for the presence of
    measures would wrongly classify any measure-container table as a fact.)
    One-to-one and many-to-many relationships don't make either side a fact
    table: neither is the "many" side of a star.

    Read errors are NOT caught. This function is the predicate of a safety
    check (`write_smart_description` refuses to document the columns of fact
    tables): returning False when `relationships.tmdl` was unreadable, locked
    by OneDrive or momentarily missing turned the check off by itself exactly
    when the model was in an uncertain state. A safety check must fail
    closed: the exception propagates and becomes an explicit tool error.
    """
    relationships = parse_relationships(project_path)
    target = (table_name or "").strip().lower()
    for rel in relationships:
        cardinalities = (rel["from_cardinality"], rel["to_cardinality"])
        if cardinalities == ("many", "one"):
            many_side = rel["from_table"]
        elif cardinalities == ("one", "many"):
            many_side = rel["to_table"]
        else:
            continue
        if (many_side or "").strip().lower() == target:
            return True
    return False
