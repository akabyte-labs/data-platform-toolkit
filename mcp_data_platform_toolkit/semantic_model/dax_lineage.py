"""
DAX lineage: the references of each measure and calculation item, resolved
against the loaded model, computed as a structure and formatted afterwards
within an output budget.

Tokenizing lives in `dax_lexer`. Here the references are resolved against
the loaded model: whether `[X]` is a measure, a column (and of which table)
or neither can only be decided knowing the model.
"""

from __future__ import annotations

from mcp_data_platform_toolkit.semantic_model import dax_lexer
from mcp_data_platform_toolkit.semantic_model.model_loader import load_semantic_model


def extract_dax_references(expression: str) -> list[tuple[str, str]]:
    """[(table_or_empty_string, element)] of the expression, without model knowledge."""
    return dax_lexer.extract_references(expression)


def _build_measure_map(model: dict) -> tuple[dict[str, str], list[str]]:
    measure_map: dict[str, str] = {}
    duplicates: list[str] = []
    for table_name in sorted(model):
        for block in model[table_name]["measures"]:
            key = block.name.strip().lower()
            if not key:
                continue
            if key in measure_map and measure_map[key] != table_name:
                duplicates.append(f"{block.name} ({measure_map[key]} / {table_name})")
                continue
            measure_map[key] = table_name
    return measure_map, duplicates


def build_model_index(model: dict) -> dict:
    """
    Lookup tables for resolving DAX references, built once per call.

    DAX is case-insensitive on object names: every key is lowercase and maps
    to the name as declared in the model, so `sales[Amount]` resolves to the
    table 'Sales' instead of producing a phantom table 'sales'.
    """
    measure_map, duplicates = _build_measure_map(model)
    column_tables: dict[str, list[str]] = {}
    for table_name in sorted(model):
        for column in model[table_name]["columns"]:
            column_tables.setdefault(column.lower(), []).append(table_name)
    return {
        "tables": {name.lower(): name for name in model},
        "measures": measure_map,
        # Declared spelling of each measure: DAX is case-insensitive, the
        # lineage reports the name as the model declares it.
        "measure_names": {
            block.name.strip().lower(): block.name.strip()
            for table_name in sorted(model) for block in model[table_name]["measures"]
            if measure_map.get(block.name.strip().lower()) == table_name
        },
        "duplicates": duplicates,
        "column_tables": column_tables,
        "table_columns": {
            name: {column.lower(): column for column in meta["columns"]}
            for name, meta in model.items()
        },
        "calc_groups": {
            name: {b.name.lower(): b.name for b in meta["calc_items"]}
            for name, meta in model.items() if meta["is_calc_group"]
        },
    }


def _column_owner(key: str, current_table: str, kind: str, context: list[str],
                  index: dict) -> tuple[str | None, list[str]]:
    """
    Table of an unqualified column reference: (table, candidates).

    `[Qty]` has no table in the text, and attributing it to the table that
    hosts the measure produced '_Measures[Qty]', a column that doesn't exist.
    The owner is the model table that has that column, preferring (in order)
    the host table for a calculated column (row context), the only candidate
    among the tables the expression iterates or filters, the host table,
    the only table in the model with that column. Anything else is
    ambiguous and is reported as such, not guessed.
    """
    candidates = index["column_tables"].get(key, [])
    if not candidates:
        return None, []
    if kind == "column" and current_table in candidates:
        return current_table, candidates
    in_context = [t for t in dict.fromkeys(context) if t in candidates]
    if len(in_context) == 1:
        return in_context[0], candidates
    if current_table in candidates:
        return current_table, candidates
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def resolve_dax_dependencies(expression: str, current_table: str, kind: str, index: dict) -> dict:
    """
    Dependencies of one expression, resolved against the model.

    {"measures", "columns", "calc_items", "unresolved": sorted lists,
     "selected_measure": bool}
    """
    analysis = dax_lexer.analyze(expression)
    tables = index["tables"]
    measures, columns, calc_items, unresolved = set(), set(), set(), set()
    context = [tables[t.lower()] for t in analysis.tables if t.lower() in tables]
    local_columns = {s.lower() for s in analysis.strings}
    referenced_groups: set[str] = set()

    for ref in analysis.references:
        key = ref.name.lower()
        table = ref.table
        if table and not ref.table_quoted and dax_lexer.is_keyword(table) and table.lower() not in tables:
            table = ""          # `RETURN [X]`, `NOT [Flag]`: not a table prefix

        if table:
            # A qualified reference is a dependency only if the model really
            # has that object. Accepting it as written produced phantom nodes:
            # `Nope[Amount]`, `Sales[NoSuchColumn]` and the column of a table
            # VARIABLE (`t[x]`) all came out as physical columns.
            owner = tables.get(table.lower())
            if owner is None:
                if not ref.table_quoted and table.lower() in analysis.variables:
                    continue    # column of a table variable: local to the expression
                unresolved.add(f"{table}[{ref.name}] (table not found in the model)")
                continue
            if index["measures"].get(key) == owner:
                measures.add(f"{owner}[{index['measure_names'][key]}]")
            elif key in index["table_columns"].get(owner, {}):
                columns.add(f"{owner}[{index['table_columns'][owner][key]}]")
                if owner in index["calc_groups"]:
                    referenced_groups.add(owner)
            elif key in index["measures"]:
                unresolved.add(f"{owner}[{ref.name}] (measure of table "
                               f"'{index['measures'][key]}', not of '{owner}')")
            else:
                unresolved.add(f"{owner}[{ref.name}] (no such column or measure in '{owner}')")
            continue

        if key in index["measures"]:
            measures.add(f"{index['measures'][key]}[{index['measure_names'][key]}]")
            continue

        owner, candidates = _column_owner(key, current_table, kind, context, index)
        if owner is not None:
            columns.add(f"{owner}[{index['table_columns'][owner][key]}]")
            if owner in index["calc_groups"]:
                referenced_groups.add(owner)
        elif key in local_columns:
            continue            # column added in the expression itself (ADDCOLUMNS, SELECTCOLUMNS)
        elif candidates:
            unresolved.add(f"[{ref.name}] (ambiguous: {', '.join(candidates)})")
        else:
            unresolved.add(f"[{ref.name}] (not found in the model)")

    # Calculation items are selected by value, as a string compared with the
    # calculation group column: `'Time Intelligence'[Time Calculation] = "YTD"`.
    # The column reference alone says which group, the string says which item.
    for group in referenced_groups:
        items = index["calc_groups"][group]
        for literal in analysis.strings:
            item = items.get(literal.strip().lower())
            if item:
                calc_items.add(f"{group}[{item}]")

    return {
        "measures": sorted(measures),
        "columns": sorted(columns),
        "calc_items": sorted(calc_items),
        "unresolved": sorted(unresolved),
        "selected_measure": bool(analysis.functions & dax_lexer.SELECTED_MEASURE_FUNCTIONS),
    }


def _has_dependencies(resolved: dict) -> bool:
    return bool(resolved["measures"] or resolved["columns"] or resolved["calc_items"]
                or resolved["unresolved"] or resolved["selected_measure"])


def build_dax_lineage(project_path: str, target_measure: str | None = None) -> dict:
    """
    Structured model lineage.

    {"elements":        [{kind, table, name, measures, columns, calc_items,
                          unresolved, selected_measure}],
     "no_dependencies": [{kind, table, name}],
     "warnings":        [...]}

    `no_dependencies` collects measures and calculation items whose expression
    references no column and no measure: constants, `BLANK()` used as a
    separator in display folders, `COUNTROWS(Table)`, `TODAY()`. They used to
    be skipped, so they appeared nowhere and a "complete" list of elements
    didn't include them: listing them separately tells the truth about what
    the model contains, distinguishing "depends on nothing" from "doesn't
    exist". A calculation item that applies `SELECTEDMEASURE()` is NOT one of
    them: it depends on whatever measure it is applied to.

    `unresolved` holds references that match no model object, or more than
    one: they are shown, never silently attributed to a table.
    """
    model = load_semantic_model(project_path)
    index = build_model_index(model)
    target = (target_measure or "").strip().lower()

    elements: list[dict] = []
    no_dependencies: list[dict] = []
    for table_name in sorted(model):
        meta = model[table_name]
        for kind, objects in (("measure", meta["measures"]), ("calculationItem", meta["calc_items"])):
            for obj in objects:
                if target and obj.name.strip().lower() != target:
                    continue
                identity = {"kind": kind, "table": table_name, "name": obj.name}
                # Read from the file that declares it: a table can be split
                # across files (partial declarations).
                expression = obj.expression()
                resolved = resolve_dax_dependencies(expression, table_name, kind, index) if expression else None
                if resolved is None or not _has_dependencies(resolved):
                    no_dependencies.append(identity)
                    continue
                elements.append({**identity, **resolved})

    warnings = []
    if index["duplicates"]:
        warnings.append("Measures with the same name in multiple tables: "
                        + ", ".join(sorted(set(index["duplicates"]))))
    unresolved_count = sum(len(e["unresolved"]) for e in elements)
    if unresolved_count:
        warnings.append(f"{unresolved_count} references could not be attributed to a single "
                        f"model object: they are listed as 'unresolved' (to verify).")
    return {"elements": elements, "no_dependencies": no_dependencies, "warnings": warnings}


LINEAGE_TITLE = "=== DAX & CALCULATION ITEM LINEAGE REPORT ==="


def _lineage_tree(elements: list[dict]) -> list[str]:
    """Level 0: full tree, one line per dependency."""
    lines: list[str] = []
    for element in elements:
        label = "Measure" if element["kind"] == "measure" else "Calc Item"
        lines.append(f"\n{label}: {element['table']}[{element['name']}]")
        if element.get("selected_measure"):
            lines.append("  ├── Applies to the measure in context (SELECTEDMEASURE)")
        if element["measures"]:
            lines.append("  ├── Depends on measures:")
            lines.extend(f"  │    └── {m}" for m in element["measures"])
        if element["calc_items"]:
            lines.append("  ├── Interacts with calculation groups / items:")
            lines.extend(f"  │    └── {c}" for c in element["calc_items"])
        if element.get("unresolved"):
            lines.append("  ├── Unresolved references (to verify):")
            lines.extend(f"  │    └── {u}" for u in element["unresolved"])
        if element["columns"]:
            lines.append("  └── Depends on physical columns:")
            lines.extend(f"       └── {c}" for c in element["columns"])
    return lines


def _lineage_single_lines(elements: list[dict], columns: str) -> list[str]:
    """
    Levels 1-3: one line per element. `columns` chooses how much detail to
    keep on physical columns, which are the bulkiest and least meaningful
    part: the business logic lives in the dependencies between measures.
    """
    lines: list[str] = []
    for element in elements:
        parts: list[str] = []
        if element.get("selected_measure"):
            parts.append("selected measure")
        if element["measures"]:
            parts.append(f"{len(element['measures'])} measures" if columns == "counts"
                         else "measures: " + ", ".join(element["measures"]))
        if element["calc_items"]:
            parts.append("calc items: " + ", ".join(element["calc_items"]))
        if element.get("unresolved"):
            parts.append("unresolved: " + ", ".join(element["unresolved"]) if columns == "names"
                         else f"{len(element['unresolved'])} unresolved")
        if element["columns"]:
            if columns == "names":
                parts.append("columns: " + ", ".join(element["columns"]))
            else:
                parts.append(f"{len(element['columns'])} columns")
        marker = "" if element["kind"] == "measure" else " (calc item)"
        dependencies = "; ".join(parts) or "no dependencies"
        lines.append(f"{element['table']}[{element['name']}]{marker} <- {dependencies}")
    return lines


def _lineage_inventory(elements: list[dict], without: list[dict]) -> list[str]:
    """
    Level 4: no dependencies, but ALL the names, grouped by table.

    It also includes the elements without dependencies, marked with '*': it
    is the only rung that can truly claim to be complete with respect to the
    model, and claiming so without them would be a lie.

    Grouping by table avoids repeating the table name on every line, which on
    thousands of elements is half of the characters.
    """
    by_table: dict[str, list[str]] = {}
    for element in elements:
        by_table.setdefault(element["table"], []).append(element["name"])
    for element in without:
        by_table.setdefault(element["table"], []).append(element["name"] + "*")

    lines = [
        f"{table} ({len(names)}): " + ", ".join(sorted(names))
        for table, names in sorted(by_table.items())
    ]
    if without:
        lines.append("(* element without dependencies: constant, BLANK(), "
                     "COUNTROWS(Table) or similar)")
    return lines


def _lineage_counts(elements: list[dict], without: list[dict]) -> list[str]:
    """Level 5: only how many elements per table. The last resort."""
    by_table: dict[str, list[int]] = {}
    for element in elements:
        by_table.setdefault(element["table"], [0, 0])[0] += 1
    for element in without:
        by_table.setdefault(element["table"], [0, 0])[1] += 1
    lines = []
    for table, (with_deps, without_deps) in sorted(by_table.items()):
        tail = f", {without_deps} without dependencies" if without_deps else ""
        lines.append(f"{table}: {with_deps} elements with dependencies{tail}")
    return lines


def _no_dependencies_section(without: list[dict], with_names: bool) -> list[str]:
    """
    Section dedicated to the elements that depend on nothing.

    It sits at the bottom of every rung except the inventory (which already
    includes them): without this section a `= BLANK()` measure appeared
    nowhere in the output, and absence from the lineage was
    indistinguishable from absence from the model. It is cheap, because only
    the name of each is listed.
    """
    if not without:
        return []
    if not with_names:
        return ["", f"--- {len(without)} elements without dependencies (names not listed) ---"]

    by_table: dict[str, list[str]] = {}
    for element in without:
        by_table.setdefault(element["table"], []).append(element["name"])
    lines = ["", f"--- Without dependencies ({len(without)}): constants, BLANK(), "
                 f"COUNTROWS(Table) or similar ---"]
    lines.extend(f"{table}: " + ", ".join(sorted(names))
                 for table, names in sorted(by_table.items()))
    return lines


def format_dax_lineage(report: dict, max_chars: int | None = None) -> str:
    """
    DAX lineage rendered in the richest form that fits the budget.

    Why a ladder and not a cut: `shape_lines` truncates the tail, and on a
    list sorted by table this silently dropped everything after a certain
    letter; on a 1,200-measure model 91% of the elements didn't appear at
    all. For an impact-analysis question, a partial answer that looks
    complete is worse than a short answer that states what is missing.

    The ladder sacrifices DETAIL first (physical columns, then the names of
    the dependencies) and only at the end the dependencies themselves, but it
    keeps THE NAME OF EVERY ELEMENT down to the last useful rung: the densest
    information, and the one needed for the next call with `target_measure`.
    It is the same principle as the ladder in `mcp_common.output_shaping`,
    applied to a text structure instead of a JSON payload.

    `max_chars=None` disables the reduction (used by tests and by callers
    that want the full rendering).
    """
    elements = report["elements"]
    without = report.get("no_dependencies", [])

    if not elements:
        if not without:
            return "No lineage found for the selected criteria."
        # This is not "nothing": these elements genuinely depend on nothing,
        # and that is an answer, not a void.
        return "\n".join(
            [LINEAGE_TITLE,
             "No element with dependencies for the selected criteria."]
            + _no_dependencies_section(without, with_names=True)
        )

    warnings = [f"\nWARNING: {w}" for w in report["warnings"]]
    names_without = _no_dependencies_section(without, with_names=True)
    count_without = _no_dependencies_section(without, with_names=False)

    # (label, HOW to build the lines, what was lost compared to the previous
    # level). The lines are functions and not ready-made lists: building every
    # rung upfront meant rendering the whole model six times (on 1,200
    # measures, ~1.4 MB of strings) to return just one, and the most common
    # case, a response that fits at the first rung, paid the most.
    ladder: list[tuple[str, "object", str]] = [
        ("full", lambda: _lineage_tree(elements) + names_without, ""),
        ("compact, one line per element",
         lambda: _lineage_single_lines(elements, "names") + names_without,
         "same information as the tree, without the indentation"),
        ("without physical column names",
         lambda: _lineage_single_lines(elements, "count") + names_without,
         "physical columns are counted, not listed"),
        ("dependency counts only",
         lambda: _lineage_single_lines(elements, "counts") + names_without,
         "dependencies are counted, not listed"),
        ("full model inventory",
         lambda: _lineage_inventory(elements, without),
         f"no dependency shown, but ALL {len(elements) + len(without)} "
         f"elements of the model are listed by name"),
        ("counts per table",
         lambda: _lineage_counts(elements, without) + count_without,
         "not even the element names fit the budget"),
    ]

    for label, build_lines, loss in ladder:
        lines = build_lines()
        body = "\n".join([LINEAGE_TITLE] + lines + warnings)
        if not loss:
            if max_chars is None or len(body) <= max_chars:
                return body
            continue
        # "No element was omitted" can only be said where it is true: the last
        # rung gives up the names, and calling it complete would be exactly
        # the defect this ladder exists to avoid.
        all_names = label != "counts per table"
        guarantee = (
            "No element was omitted: they are all here, with less detail."
            if all_names else
            "WARNING: at this level the NAMES of the elements are not listed."
        )
        note = (
            f"\n[response reduced ({label}): {len(elements) + len(without)} elements, "
            f"{loss}. {guarantee} "
            f"For the full detail of a single element call "
            f"get_dax_lineage with 'target_measure'.]"
        )
        # The note must be COUNTED in the budget, not added after checking it:
        # on a small model it is worth more than the body itself and made the
        # very response that should respect the limit exceed it.
        if max_chars is None or len(body) + len(note) <= max_chars:
            return body + note

    # Even the counts alone exceed the budget: it is a huge model (or an
    # absurdly small budget) and the only honest answer is to say how big it
    # is and how to narrow it down. This message is the FLOOR: its length is
    # never reduced further, because truncating "it doesn't fit" helps nobody.
    # It is the only case where the response can exceed `max_chars`.
    return (
        f"{LINEAGE_TITLE}\n"
        f"{len(elements)} elements with dependencies across "
        f"{len({e['table'] for e in elements})} tables: not even the list of names fits "
        f"the budget.\nCall get_dax_lineage with 'target_measure' for a single "
        f"element, or proceed table by table with 'read_table_definition'."
        + "".join(warnings)
    )


def get_dax_lineage_logic(project_path: str, target_measure: str | None = None) -> str:
    """Compatibility helper: lineage already formatted as text."""
    return format_dax_lineage(build_dax_lineage(project_path, target_measure))
