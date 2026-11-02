"""
Translations of the semantic model, from its 'cultures/' folder: the caption,
description and display folder of the model's objects in each language, how
many of the visible objects are translated, and the translations of objects
that no longer exist.

The model loader skips 'cultures/': linguistic metadata is often the largest
part of a model and says nothing about how it is computed. This module reads
those files only when translations are asked for.

TMDL shape (cultures/<culture>.tmdl), as documented by Microsoft; Power BI
Desktop writes `cultureInfo` where the documentation shows `culture`:

    cultureInfo en-GB
        translations
            model Model
                caption: Data model
                table Sales
                    caption: Turnover
                    measure 'Sales Amount'
                        caption: Turnover value
                        displayFolder: Figures
        linguisticMetadata = ...
"""

from __future__ import annotations

import os

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.model_loader import find_table, load_definition
from mcp_data_platform_toolkit.semantic_model.project_paths import find_definition_folder
from mcp_data_platform_toolkit.semantic_model.tmdl_io import read_tmdl_lines
from mcp_data_platform_toolkit.semantic_model.tmdl_parser import ElementNotFoundError

TRANSLATED_PROPERTIES = ("caption", "description", "displayFolder")
_OBJECT_KINDS = ("column", "measure", "hierarchy")


def _properties(lines: list[str], block) -> dict[str, str]:
    values = {}
    for name in TRANSLATED_PROPERTIES:
        child = tmdl.get_property(lines, block, name)
        if child is not None and child.value:
            values[name] = tmdl.normalize_text_value(child.value)
    return values


def _read_culture(file_path: str) -> dict | None:
    """{"culture", "file", "model": {props}, "tables": {name: {"props", "objects"}}} or None."""
    lines = read_tmdl_lines(file_path)
    blocks = tmdl.find_all_blocks(lines)
    culture = next((b for b in blocks if not b.indent and b.keyword in ("cultureInfo", "culture")), None)
    if culture is None:
        return None
    result = {"culture": culture.name.strip(), "file": file_path, "model": {}, "tables": {},
              "linguistic_metadata": any(b.keyword == "linguisticMetadata" for b in blocks)}
    for section in tmdl.child_blocks(lines, culture, "translations"):
        for model in tmdl.child_blocks(lines, section, "model"):
            result["model"].update(_properties(lines, model))
            for table in tmdl.child_blocks(lines, model, "table"):
                entry = result["tables"].setdefault(table.name.strip(), {"props": {}, "objects": []})
                entry["props"].update(_properties(lines, table))
                for kind in _OBJECT_KINDS:
                    for child in tmdl.child_blocks(lines, table, kind):
                        props = _properties(lines, child)
                        if props:
                            entry["objects"].append((kind, child.name.strip(), props))
    return result


def _culture_files(project_path: str) -> list[str]:
    folder = os.path.join(find_definition_folder(project_path), "cultures")
    if not os.path.isdir(folder):
        return []
    return [os.path.join(folder, name) for name in sorted(os.listdir(folder))
            if name.lower().endswith(".tmdl")]


def _model_culture(project_path: str) -> str:
    """The model's own culture (`culture:` in model.tmdl), '' if not set."""
    path = os.path.join(find_definition_folder(project_path), "model.tmdl")
    if not os.path.isfile(path):
        return ""
    lines = read_tmdl_lines(path)
    for block in tmdl.find_all_blocks(lines):
        if block.keyword == "model" and not block.indent:
            child = tmdl.get_property(lines, block, "culture")
            return tmdl.normalize_text_value(child.value) if child and child.value else ""
    return ""


def _visible_objects(tables: dict) -> dict[str, list[tuple[str, str]]]:
    """{table: [(kind, name)]} of what a report user sees: visible tables and their visible objects."""
    visible: dict[str, list[tuple[str, str]]] = {}
    for name, meta in tables.items():
        if any(tmdl.is_hidden(p["lines"], p["table_block"]) for p in meta["parts"]):
            continue
        items = [("table", name)]
        items += [("column", o.name) for o in meta["column_objects"] if not tmdl.is_hidden(o.lines, o.block)]
        items += [("measure", o.name) for o in meta["measures"] if not tmdl.is_hidden(o.lines, o.block)]
        for part in meta["parts"]:
            items += [("hierarchy", b.name) for b in tmdl.child_blocks(part["lines"], part["table_block"], "hierarchy")
                      if not tmdl.is_hidden(part["lines"], b)]
        visible[name] = items
    return visible


def _analyze(culture: dict, tables: dict, visible: dict) -> dict:
    """Coverage of one culture against the model, and its translations of missing objects."""
    captions: set[tuple[str, str, str]] = set()        # (table lower, kind, name lower)
    orphans: list[str] = []
    counts = dict.fromkeys(TRANSLATED_PROPERTIES, 0)
    by_table: dict[str, dict] = {}

    for table_name, entry in culture["tables"].items():
        meta = find_table(tables, table_name)
        if meta is None:
            orphans.append(f"{table_name} (table)")
            continue
        declared = {
            "column": {c.lower() for c in meta["columns"]},
            "measure": {m.name.strip().lower() for m in meta["measures"]},
            "hierarchy": {h.lower() for h in meta["hierarchies"]},
        }
        by_table[meta["name"]] = entry
        for prop in entry["props"]:
            counts[prop] += 1
        if "caption" in entry["props"]:
            captions.add((meta["name"].lower(), "table", meta["name"].lower()))
        for kind, name, props in entry["objects"]:
            if name.lower() not in declared[kind]:
                orphans.append(f"{meta['name']}[{name}] ({kind})")
                continue
            for prop in props:
                counts[prop] += 1
            if "caption" in props:
                captions.add((meta["name"].lower(), kind, name.lower()))

    total = sum(len(items) for items in visible.values())
    translated = sum(1 for table, items in visible.items() for kind, name in items
                     if (table.lower(), kind, name.lower()) in captions)
    missing = {table: [(kind, name) for kind, name in items
                       if (table.lower(), kind, name.lower()) not in captions]
               for table, items in visible.items()}
    return {"total": total, "translated": translated, "counts": counts, "orphans": orphans,
            "missing": missing, "by_table": by_table}


def _coverage(analysis: dict) -> str:
    total, translated = analysis["total"], analysis["translated"]
    percent = f" ({translated * 100 // total}%)" if total else ""
    text = f"captions for {translated} of {total} visible objects{percent}"
    extra = [f"{analysis['counts'][p]} {one if analysis['counts'][p] == 1 else many}"
             for p, one, many in (("description", "description", "descriptions"),
                                  ("displayFolder", "display folder", "display folders"))
             if analysis["counts"][p]]
    if extra:
        text += ", " + ", ".join(extra)
    if analysis["orphans"]:
        text += f"; {len(analysis['orphans'])} for objects not in the model"
    return text


def _quoted(props: dict) -> str:
    parts = []
    if "caption" in props:
        parts.append(f"'{props['caption']}'")
    if "description" in props:
        parts.append(f"description: {props['description']}")
    if "displayFolder" in props:
        parts.append(f"folder: {props['displayFolder']}")
    return " | ".join(parts)


def translations_report(project_path: str, culture: str = "", table_name: str = "") -> str:
    tables = load_definition(project_path)["tables"]
    cultures = [c for c in (_read_culture(path) for path in _culture_files(project_path)) if c]
    model_culture = _model_culture(project_path)
    if not cultures:
        return ("No cultures in the model ('definition/cultures' is missing or empty): "
                f"no translations{f'; the model culture is {model_culture}' if model_culture else ''}.")

    visible = _visible_objects(tables)
    if not culture:
        lines = [f"=== TRANSLATIONS ({len(cultures)} culture{'s' if len(cultures) != 1 else ''}"
                 + (f"; model culture {model_culture}" if model_culture else "") + ") ==="]
        for item in cultures:
            if not item["tables"] and not item["model"]:
                kind = "linguistic metadata only" if item["linguistic_metadata"] else "empty"
                lines.append(f"  {item['culture']}: no translations ({kind})")
            else:
                lines.append(f"  {item['culture']}: {_coverage(_analyze(item, tables, visible))}")
        lines.append("Call again with 'culture' for the translations and the objects still missing one.")
        return "\n".join(lines)

    item = next((c for c in cultures if c["culture"].lower() == culture.strip().lower()), None)
    if item is None:
        raise ElementNotFoundError(
            f"Culture '{culture}' not found in the model (cultures: "
            f"{', '.join(c['culture'] for c in cultures)}).")

    selected = None
    if (table_name or "").strip():
        meta = find_table(tables, table_name)
        if meta is None:
            raise FileNotFoundError(f"Table '{table_name}' not found in the semantic model.")
        selected = meta["name"]

    analysis = _analyze(item, tables, visible)
    lines = [f"=== TRANSLATIONS {item['culture']}: {_coverage(analysis)} ==="]
    if item["model"] and selected is None:
        lines.append(f"Model: {_quoted(item['model'])}")

    for name in sorted(set(visible) | set(analysis["by_table"]), key=str.lower):
        if selected is not None and name != selected:
            continue
        entry = analysis["by_table"].get(name, {"props": {}, "objects": []})
        header = f"\n{name}" + (f" -> {_quoted(entry['props'])}" if entry["props"] else "")
        if name not in visible:
            header += "  (hidden table)"
        lines.append(header)
        for kind, object_name, props in entry["objects"]:
            stale = f"{name}[{object_name}] ({kind})" in analysis["orphans"]
            lines.append(f"  {kind} {object_name} -> {_quoted(props)}"
                         + ("  [!] not in the model" if stale else ""))
        missing = [f"{kind} {n}" for kind, n in analysis["missing"].get(name, []) if kind != "table"]
        if name in visible and ("table", name) in analysis["missing"].get(name, []):
            missing.insert(0, "the table itself")
        if missing:
            lines.append("  no caption: " + ", ".join(missing))

    orphans = analysis["orphans"]
    if selected is not None:
        orphans = [o for o in orphans if o.lower().startswith(selected.lower() + "[")]
    if orphans:
        lines.append("\n[!] translations of objects not in the model: " + ", ".join(orphans))
    return "\n".join(lines)
