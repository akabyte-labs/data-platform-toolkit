"""
Analysis of Power BI reports in Enhanced Metadata / PBIR format
('.Report' folders with definition/pages/*/visuals/*.json).

Complements semantic_model/reader_tools.py (model side): this module covers
the consumption side, i.e. where the model's measures and columns are
actually used.

Design notes:
  - a single scan of the files for N fields, instead of one scan per field;
  - textual references (queryRef, Restatement) are detected, not only
    {"Measure": {"Property": ...}} bindings, and reported as "to verify";
  - besides visuals and filters, report-level measures (reportExtensions),
    bookmarks and drillthrough/tooltip page bindings are read: a field used
    only there is used, and reporting it as unused is the wrong answer to
    "can I delete it?";
  - reports in the PBIR-Legacy format are NOT analyzed, and the response
    says so: a silent "NO usages" on a report that was never read is the
    most dangerous answer this tool can give;
  - the RLS filters of the local models behind the reports are read too: a
    column only a security role reads is not "unused", and deleting it
    breaks the role;
  - errors become MCP responses with isError=True.
"""

from __future__ import annotations

import json
import os
import re

from mcp_data_platform_toolkit.mcp_common.errors import register_tools
from mcp_data_platform_toolkit.mcp_common.output_shaping import MAX_TOOL_OUTPUT_CHARS, shape_lines
from mcp_data_platform_toolkit.reports.pbir_references import (
    MATCH_TEXT,
    build_level_map,
    collect_pbir_references,
    dax_reference_index,
    match_references,
)
from mcp_data_platform_toolkit.semantic_model.model_loader import load_semantic_model
from mcp_data_platform_toolkit.semantic_model.rls import rls_references
from mcp_data_platform_toolkit.semantic_model.project_paths import (
    find_report_folders,
    find_semantic_model_folder,
    pbir_model_path,
    read_dataset_reference,
)

# A pathological PBIR file (extreme nesting, malformed JSON, permissions) must
# not make the analysis of the whole workspace fail: these errors are isolated
# per file and reported at the end. `RecursionError` in particular escaped the
# catch and took down the whole scan, which is exactly the opposite of what
# the `unreadable` list is for.
PER_FILE_ERRORS = (json.JSONDecodeError, OSError, RecursionError, UnicodeDecodeError, ValueError)

LEGACY_MESSAGE = "NOT analyzed, upgrade to PBIR"
UNRECOGNIZED_MESSAGE = "NOT analyzed, no report definition found"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _read_json(file_path: str) -> dict:
    """
    A PBIR file, whose top level is always a JSON object.

    Valid JSON with another top level (a list, a string) used to reach the
    callers' `.get()` and raise AttributeError, which is not a per-file
    error: one such file took down the whole workspace scan. Since Python
    3.12 even a deeply nested list parses instead of raising RecursionError,
    so this was the path a pathological file actually took. A ValueError
    keeps it among the unreadable files.
    """
    with open(file_path, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"top level is a JSON {type(data).__name__}, not an object")
    return data


def _report_name(report_folder: str) -> str:
    base = os.path.basename(report_folder)
    return base[: -len(".Report")] if base.endswith(".Report") else base


def _page_name(page_folder: str) -> str:
    """Prefers the displayName from page.json, otherwise the folder id."""
    page_json_path = os.path.join(page_folder, "page.json")
    if os.path.exists(page_json_path):
        try:
            display_name = _read_json(page_json_path).get("displayName")
            if display_name:
                return display_name
        except PER_FILE_ERRORS:
            pass
    return os.path.basename(page_folder)


# ---------------------------------------------------------------------------
# Report format and semantic model binding
# ---------------------------------------------------------------------------

def report_format(report_folder: str) -> str:
    """'pbir' | 'legacy' | 'unknown'."""
    if os.path.isdir(os.path.join(report_folder, "definition")):
        return "pbir"
    if os.path.isfile(os.path.join(report_folder, "report.json")):
        return "legacy"
    return "unknown"


def _model_name_from_folder(folder: str) -> str:
    base = os.path.basename(os.path.normpath(folder))
    return base[: -len(".SemanticModel")] if base.endswith(".SemanticModel") else base


_CATALOG_RE = re.compile(r"initial\s+catalog\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^;]*))", re.IGNORECASE)


def _catalog_from_connection(connection_string: str) -> str:
    match = _CATALOG_RE.search(connection_string or "")
    if not match:
        return ""
    return next((g for g in match.groups() if g is not None), "").strip()


def read_report_binding(report_folder: str) -> dict:
    """
    Semantic model a report is bound to, from its 'definition.pbir'.

    {"mode": "byPath" | "byConnection" | "unknown", "model_folder", "model_name"}
    byPath points to a local '.SemanticModel' folder; byConnection is a live
    connection, identified by the dataset name ('initial catalog').
    """
    info = {"mode": "unknown", "model_folder": "", "model_name": ""}
    folder = pbir_model_path(report_folder)
    if folder:
        return {"mode": "byPath", "model_folder": folder, "model_name": _model_name_from_folder(folder)}

    by_connection = read_dataset_reference(report_folder).get("byConnection")
    if isinstance(by_connection, dict):
        name = _catalog_from_connection(str(by_connection.get("connectionString") or ""))
        return {"mode": "byConnection", "model_folder": "", "model_name": name}
    return info


def _resolve_model_filter(semantic_model: str) -> dict | None:
    """The model the caller restricts the search to: a path (.pbip / folder) or a name."""
    text = (semantic_model or "").strip()
    if not text:
        return None
    if os.path.exists(text):
        folder = find_semantic_model_folder(text)
        return {"folder": folder, "name": _model_name_from_folder(folder)}
    return {"folder": "", "name": _model_name_from_folder(text)}


def _same_folder(first: str, second: str) -> bool:
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def _binding_matches(binding: dict, model_filter: dict) -> bool | None:
    """True / False, or None when the binding can't be verified."""
    if binding["mode"] == "byPath":
        same_name = binding["model_name"].lower() == model_filter["name"].lower()
        if not model_filter["folder"]:
            return same_name
        if os.path.isdir(binding["model_folder"]):
            # Both folders are on disk: the path decides. A namesake model in
            # another folder used to count as the same model.
            return _same_folder(binding["model_folder"], model_filter["folder"])
        # The bound folder isn't on this disk (moved, another checkout): a
        # matching name is a guess, not a verification.
        return None if same_name else False
    if binding["mode"] == "byConnection" and binding["model_name"]:
        return binding["model_name"].lower() == model_filter["name"].lower()
    return None


def _level_map_for(model_folder: str) -> dict | None:
    """Hierarchy level -> column of a local model, or None if it can't be read."""
    if not model_folder or not os.path.isdir(model_folder):
        return None
    try:
        return build_level_map(load_semantic_model(model_folder))
    except Exception:  # noqa: BLE001 - without the map levels stay "to verify"
        return None


# ---------------------------------------------------------------------------
# Artifacts of a PBIR report
# ---------------------------------------------------------------------------

def _iter_report_artifacts(report_folder: str):
    """
    Yields (artifact, json_path) for every analyzable file.

    `artifact` keeps report and page separate (useful to callers that
    aggregate by page) next to the context label ready for the text output.
    `scope` restricts the search to one key of the file; `dax` marks the
    report-level measures, whose references are DAX text, not bindings.
    """
    report_name = _report_name(report_folder)
    definition_folder = os.path.join(report_folder, "definition")

    # Report-level filters. In current PBIR they live inside 'report.json'
    # under the 'filterConfig' key; 'filters.json' is the format of earlier
    # versions and is still supported. Looking only for the latter meant
    # reading no report filter at all.
    for file_name, scope in (("report.json", "filterConfig"), ("filters.json", "")):
        path = os.path.join(definition_folder, file_name)
        if os.path.exists(path):
            yield (
                {"report": report_name, "page": "", "kind": "filter", "scope": scope,
                 "context": f"{report_name} | (report-level filter)"},
                path,
            )

    # Report-level measures: DAX written in the report. A model measure used
    # only by one of them used to come out as "NO usages".
    extensions = os.path.join(definition_folder, "reportExtensions.json")
    if os.path.exists(extensions):
        yield ({"report": report_name, "page": "", "kind": "report_measure", "dax": True,
                "context": f"{report_name} | (report-level measure)"}, extensions)

    bookmarks_folder = os.path.join(definition_folder, "bookmarks")
    if os.path.isdir(bookmarks_folder):
        for file_name in sorted(os.listdir(bookmarks_folder)):
            if file_name.lower().endswith(".bookmark.json"):
                yield ({"report": report_name, "page": "", "kind": "bookmark",
                        "context": f"{report_name} | (bookmark '{file_name[:-len('.bookmark.json')]}')"},
                       os.path.join(bookmarks_folder, file_name))

    pages_folder = os.path.join(definition_folder, "pages")
    if not os.path.isdir(pages_folder):
        return

    for page_id in sorted(os.listdir(pages_folder)):
        page_folder = os.path.join(pages_folder, page_id)
        if not os.path.isdir(page_folder):
            continue
        page_name = _page_name(page_folder)

        # Same story at page level: 'page.json' with 'filterConfig' in current
        # PBIR, 'filters.json' in earlier versions. 'pageBinding' holds the
        # fields of a drillthrough or tooltip page: outside 'filterConfig',
        # they were never read.
        for file_name, scope, label in (
            ("page.json", "filterConfig", "page-level filter"),
            ("page.json", "pageBinding", "drillthrough/tooltip field"),
            ("filters.json", "", "page-level filter"),
        ):
            path = os.path.join(page_folder, file_name)
            if os.path.exists(path):
                yield (
                    {"report": report_name, "page": page_name, "kind": "filter", "scope": scope,
                     "context": f"{report_name} | {page_name} | ({label})"},
                    path,
                )

        visuals_folder = os.path.join(page_folder, "visuals")
        if not os.path.isdir(visuals_folder):
            continue
        for visual_id in sorted(os.listdir(visuals_folder)):
            visual_json = os.path.join(visuals_folder, visual_id, "visual.json")
            if os.path.exists(visual_json):
                yield (
                    {"report": report_name, "page": page_name, "kind": "visual",
                     "visual_id": visual_id,
                     "context": f"{report_name} | {page_name} | visual '{visual_id}'"},
                    visual_json,
                )


def _report_measure_indexes(data, artifact: dict) -> list[tuple[str, dict]]:
    """(label, index) for each measure defined in 'reportExtensions.json'."""
    result = []
    for entity in (data.get("entities") or []) if isinstance(data, dict) else []:
        if not isinstance(entity, dict):
            continue
        for measure in entity.get("measures") or []:
            if not isinstance(measure, dict) or not isinstance(measure.get("expression"), str):
                continue
            label = f"{artifact['report']} | (report-level measure '{measure.get('name', '?')}')"
            result.append((label, dax_reference_index(measure["expression"])))
    return result


# ---------------------------------------------------------------------------
# Usage search
# ---------------------------------------------------------------------------

def field_key(target: dict) -> str:
    """Readable identifier of a target: 'Measure' or 'Table[Column]'."""
    if (target.get("kind") or "measure").strip().lower() == "column":
        table = (target.get("table") or "").strip()
        name = (target.get("name") or "").strip()
        return f"{table}[{name}]" if table else name
    return (target.get("name") or "").strip()


def parse_column_ref(reference: str) -> dict:
    """'Table[Column]' -> column target. Without brackets the table stays empty."""
    text = (reference or "").strip()
    if text.endswith("]") and "[" in text:
        table, column = text[:-1].split("[", 1)
        table = table.strip()
        if len(table) >= 2 and table.startswith("'") and table.endswith("'"):
            table = table[1:-1].replace("''", "'")
        return {"kind": "column", "table": table, "name": column.strip()}
    return {"kind": "column", "table": "", "name": text}


def _select_reports(report_folders: list[str], model_filter: dict | None) -> dict:
    """
    Splits the report folders into analyzable ones and the ones that are not.

    {"analyzed": [(folder, binding)], "legacy": [names], "unknown_format": [names],
     "other_model": [(name, model)], "unverified": [names]}
    """
    selection = {"analyzed": [], "legacy": [], "unknown_format": [], "other_model": [], "unverified": []}
    for folder in report_folders:
        name = _report_name(folder)
        fmt = report_format(folder)
        if fmt == "legacy":
            selection["legacy"].append(name)
            continue
        if fmt == "unknown":
            selection["unknown_format"].append(name)
            continue
        binding = read_report_binding(folder)
        if model_filter is not None:
            matches = _binding_matches(binding, model_filter)
            if matches is False:
                selection["other_model"].append((name, binding["model_name"] or "?"))
                continue
            if matches is None:
                selection["unverified"].append(name)
        selection["analyzed"].append((folder, binding))
    return selection


def collect_field_usage(reports_workspace_path: str, targets: list[dict], semantic_model: str = "") -> dict:
    """
    Usages of measures AND columns in PBIR reports, in structured form.

    A single scan of the files for N fields. It is the core logic behind
    `find_measure_usage_in_reports`, which formats it as text.

    Structure: {"fields": [key, ...], "usages": {key: [artifact, ...]},
                "unreadable": [...], "reports": selection}, where each artifact
    has report, page, kind, a ready-made label and match (MATCH_BINDING |
    MATCH_TEXT), and `selection` is the outcome of `_select_reports`.
    """
    valid: list[dict] = []
    seen: set[str] = set()
    for target in targets or []:
        key = field_key(target)
        if not key or key in seen:
            continue
        seen.add(key)
        valid.append(target)
    if not valid:
        raise ValueError("Specify at least one measure or column to search for.")

    model_filter = _resolve_model_filter(semantic_model)
    selection = _select_reports(find_report_folders(reports_workspace_path), model_filter)

    keys = [field_key(t) for t in valid]
    usages: dict[str, list[dict]] = {key: [] for key in keys}
    unreadable: list[str] = []
    level_maps: dict[str, dict | None] = {}
    analyzed: list[tuple[str, dict]] = []

    for report_folder, binding in selection["analyzed"]:
        # A 'definition' folder is not enough to call a report analyzed: a
        # half-converted or emptied one yields no file at all, and counting
        # it would bring back the silent "NO usages" on a report never read.
        read_any = False
        # The model behind the report tells which column a hierarchy level
        # really is. Local binding first, then the model the caller named.
        model_folder = binding["model_folder"] or (model_filter or {}).get("folder", "")
        if model_folder not in level_maps:
            level_maps[model_folder] = _level_map_for(model_folder)
        level_map = level_maps[model_folder]

        for artifact, json_path in _iter_report_artifacts(report_folder):
            read_any = True
            try:
                data = _read_json(json_path)

                if artifact.get("dax"):
                    indexes = _report_measure_indexes(data, artifact)
                else:
                    # For page.json/report.json the search is ONLY inside the
                    # scoped key: the rest of the file holds the page
                    # displayName and layout properties, which would give
                    # false positives on measure names.
                    scope = artifact.get("scope")
                    if scope:
                        data = data.get(scope)
                        if not data:
                            continue
                    label = artifact["context"]
                    if artifact["kind"] == "visual":
                        visual = data.get("visual") if isinstance(data, dict) else None
                        visual_type = visual.get("visualType") if isinstance(visual, dict) else None
                        label = f"{label} (type: {visual_type or 'unknown'})"
                    # ONE pass over the document, then N set lookups.
                    indexes = [(label, collect_pbir_references(data))]
            except PER_FILE_ERRORS as exc:
                unreadable.append(f"{artifact['context']}: {type(exc).__name__}")
                continue

            for label, index in indexes:
                for target in valid:
                    outcome = match_references(index, target, level_map)
                    if outcome:
                        usages[field_key(target)].append(
                            {**artifact, "label": label, "match": outcome}
                        )

        if read_any:
            analyzed.append((report_folder, binding))
        else:
            selection["unknown_format"].append(_report_name(report_folder))
    selection["analyzed"] = analyzed

    if not analyzed and (selection["legacy"] or selection["unknown_format"]):
        raise ValueError(
            "No report could be analyzed, so nothing can be said about the usage of these "
            "fields.\n" + "\n".join(_coverage_lines(selection))
        )

    folders = [(model_filter or {}).get("folder", "")] + [b["model_folder"] for _f, b in analyzed]
    rls, rls_problems, rls_checked = _rls_usages(folders, valid)
    return {"fields": keys, "usages": usages, "unreadable": unreadable, "reports": selection,
            "rls": rls, "rls_problems": rls_problems, "rls_checked": rls_checked}


def _split_ref(reference: str) -> tuple[str, str]:
    """'Sales[Amount]' -> ('sales', 'amount')."""
    table, _bracket, rest = reference.partition("[")
    return table.strip().lower(), (rest[:-1] if rest.endswith("]") else rest).strip().lower()


def _rls_reads(reference: dict, target: dict) -> bool:
    """True if an RLS filter (see `rls_references`) reads the target field."""
    name = (target.get("name") or "").strip().lower()
    if (target.get("kind") or "measure").strip().lower() == "column":
        table = (target.get("table") or "").strip().lower()
        return any(c == name and (not table or t == table)
                   for t, c in map(_split_ref, reference["columns"]))
    return any(m == name for _t, m in map(_split_ref, reference["measures"]))


def _rls_usages(model_folders: list[str], targets: list[dict]) -> tuple[dict, list[str], bool]:
    """
    ({field key: ["role 'R', filter on 'T' (model M)"]}, problems, checked) for
    the RLS filters of the local models among `model_folders`. `checked` is
    False when there was no local model to read: the caller says so, since
    "no RLS usage" would otherwise be indistinguishable from "not looked at".
    """
    usages: dict[str, list[str]] = {field_key(t): [] for t in targets}
    problems: list[str] = []
    seen: set[str] = set()
    for folder in model_folders:
        if not folder or not os.path.isdir(folder):
            continue
        key = os.path.normcase(os.path.realpath(folder))
        if key in seen:
            continue
        seen.add(key)
        model = _model_name_from_folder(folder)
        try:
            references = rls_references(folder)
        except Exception as exc:  # noqa: BLE001 - the report usages stay valid without it
            problems.append(f"{model}: {type(exc).__name__}: {exc}")
            continue
        for reference in references:
            for target in targets:
                if _rls_reads(reference, target):
                    usages[field_key(target)].append(
                        f"role '{reference['role']}', filter on '{reference['table']}' (model {model})")
    return usages, problems, bool(seen)


def collect_measure_usage(reports_workspace_path: str, measure_names: list[str]) -> dict:
    """Compatibility helper: measures only, with the 'measures' key of the earlier format."""
    names = [n.strip() for n in (measure_names or []) if n and n.strip()]
    if not names:
        raise ValueError("Specify at least one measure in 'measure_name' or 'measure_names'.")
    outcome = collect_field_usage(
        reports_workspace_path, [{"kind": "measure", "name": n} for n in names])
    return {"measures": outcome["fields"], "usages": outcome["usages"],
            "unreadable": outcome["unreadable"]}


def find_measure_usage_in_reports(
    reports_workspace_path: str,
    measure_name: str = "",
    measure_names: list[str] | None = None,
    column_names: list[str] | None = None,
    semantic_model: str = "",
) -> str:
    """
    Finds which reports and pages use one or more measures (and, with
    `column_names`, columns too), by reading Power BI reports in Enhanced
    Metadata (PBIR) format. Analyzed: visuals, report-, page- and
    visual-level filters, drillthrough/tooltip page fields, bookmarks and
    report-level measures (DAX defined in the report).

    Reports in the PBIR-Legacy format are NOT analyzed: the response lists
    them, and they must be upgraded to PBIR before their usages can be known.

    The Row-Level Security filters of the semantic models behind the reports
    (bound by path, or named in `semantic_model` as a path) are read too: a
    field read by a role's filter is reported with the role, since removing
    it breaks the security.

    IMPORTANT: reports in live connection to the semantic model are NOT
    located in the model's folder. Pass the common "workspace" folder that
    contains the report projects (each with its own .pbip and '.Report' folder).

    USE THIS TOOL to answer "where is measure X used" or to assess the impact
    of changing/removing a measure on existing reports, as a complement to
    'get_dax_lineage', which only covers the semantic model side.
    Passing several names in `measure_names` reads the files only once.

    The response starts with a summary that ALWAYS lists every searched field
    with its usage count (or "NO usages"); the per-visual detail follows while
    it fits the output budget. Matches found only in a text field (title,
    queryRef) are flagged "to verify": they may be a namesake.

    Args:
        reports_workspace_path: Absolute path to the workspace folder containing
            the report projects (each with its own '.Report' folder).
        measure_name: Exact name of the measure to search for (case-insensitive).
        measure_names: Optional list of measures to search for in the same scan.
        column_names: Columns to search for, in the form 'Table[Column]'. A column
            can be used by a report directly (axis, slicer, hierarchy level,
            filter) without any measure reading it: searching only among
            measures would report "no usages" for a column that is actually used.
        semantic_model: Optional semantic model the fields belong to (path to
            its .pbip / '.SemanticModel' folder, or its name). Reports bound to
            a different model (per their 'definition.pbir') are skipped, so a
            namesake measure of another model is not counted.
    """
    targets = [{"kind": "measure", "name": n}
               for n in [measure_name] + list(measure_names or []) if n and n.strip()]
    targets += [parse_column_ref(c) for c in (column_names or []) if c and c.strip()]
    if not targets:
        raise ValueError(
            "Specify at least one measure ('measure_name'/'measure_names') "
            "or one column ('column_names', format 'Table[Column]')."
        )

    report = collect_field_usage(reports_workspace_path, targets, semantic_model)
    return _format_usages(report, MAX_TOOL_OUTPUT_CHARS)


def _coverage_lines(selection: dict) -> list[str]:
    """What was NOT analyzed. Part of the summary: it must survive every reduction."""
    lines = []
    legacy = selection.get("legacy") or []
    unrecognized = selection.get("unknown_format") or []
    if legacy:
        lines.append(f"[!] {_plural(len(legacy), 'PBIR-Legacy report')} {LEGACY_MESSAGE}: "
                     + ", ".join(legacy))
    if unrecognized:
        lines.append(f"[!] {_plural(len(unrecognized), 'report folder')} {UNRECOGNIZED_MESSAGE}: "
                     + ", ".join(unrecognized))
    if selection.get("other_model"):
        lines.append("Skipped, bound to another semantic model: "
                     + ", ".join(f"{name} -> {model}" for name, model in selection["other_model"]))
    if selection.get("unverified"):
        lines.append("Analyzed without being able to verify the semantic model they are bound to: "
                     + ", ".join(selection["unverified"]))
    return lines


def _format_usages(report: dict, max_chars: int | None = None) -> str:
    """
    Summary for every searched field, then the detail while the budget allows.

    The order is not cosmetic. Cutting the full list by lines made the last
    searched fields disappear entirely: the response didn't name them at all,
    and "doesn't appear in the output" is indistinguishable from "not used",
    exactly the wrong conclusion on which one decides whether a measure can be
    removed. The summary is one line per field and practically always fits
    the budget: this way the answer to "is it used? how many times?" is
    complete for ALL fields, and only the list of visuals can be lost, which
    can be recovered by calling the tool on one field at a time.
    """
    fields = report["fields"]
    selection = report.get("reports") or {}

    # Every count is only true of the reports that were read: "NO usages"
    # AND "2 usages" alike. Qualifying only the first let a count look
    # complete while a PBIR-Legacy report could hold more usages.
    where = " in the analyzed reports" if (selection.get("legacy") or selection.get("unknown_format")) else ""

    header = f"=== SUMMARY ({_plural(len(fields), 'field')} searched"
    if "analyzed" in selection:
        header += f", {_plural(len(selection['analyzed']), 'report')} analyzed"
    summary = [header + ") ==="] + _coverage_lines(selection)
    if report.get("rls_checked") is False:
        summary.append("RLS filters not checked: no local semantic model behind these reports "
                       "(pass its path in 'semantic_model').")
    rls = report.get("rls") or {}

    total_probable = 0
    for name in fields:
        occurrences = report["usages"][name]
        probable = sum(1 for a in occurrences if a.get("match") == MATCH_TEXT)
        total_probable += probable
        # Part of the summary, not of the detail: it must survive every
        # reduction, like the counts.
        security = f"; read by RLS: {'; '.join(rls[name])}" if rls.get(name) else ""
        if not occurrences:
            summary.append(f"  '{name}': NO usages found{where}{security}")
            continue
        certain = len(occurrences) - probable
        counts = _plural(len(occurrences), "usage") + where
        if probable:
            counts += f" ({certain} certain, {probable} to verify)"
        summary.append(f"  '{name}': {counts}{security}")

    tail: list[str] = []
    if total_probable:
        # Without this distinction, a visual titled 'Margin' made the 'Margin'
        # measure look "used", and a column 'Sales.Total' made a namesake
        # measure look used. On a "can I delete it?" decision it is the
        # difference between an answer and a mistake.
        tail.append(
            "NOTE: 'to verify' = the name appears only in a text field of the report "
            "(title, queryRef, conditional formatting), as an unqualified reference "
            "in a report-level measure, or as a hierarchy level whose column could not "
            "be read from the model: it may be a namesake. 'certain' matches are real "
            "bindings."
        )
    if report["unreadable"]:
        tail.append("WARNING: unreadable files: " + "; ".join(report["unreadable"]))
    if report.get("rls_problems"):
        tail.append("WARNING: RLS filters not read for: " + "; ".join(report["rls_problems"]))

    detail: list[str] = []
    for name in fields:
        occurrences = report["usages"][name]
        if not occurrences:
            continue
        detail.append("")
        detail.append(f"=== '{name}' — {_plural(len(occurrences), 'usage')} ===")
        detail.extend(
            artifact["label"] + (" [to verify: text match only]"
                                 if artifact.get("match") == MATCH_TEXT else "")
            for artifact in occurrences
        )

    full = "\n".join(summary + detail + ([""] + tail if tail else [])).strip()
    if max_chars is None or len(full) <= max_chars:
        return full

    # The detail doesn't fit. Step down by priority, because the summary IS
    # the answer to the question and must survive everything else:
    #   1. summary + warnings + note   2. summary + note   3. summary only
    note = (
        "[detail omitted: the counts above are COMPLETE for all searched "
        "fields, only the list of usage points is missing. Call the tool on "
        "one field at a time to get it.]"
    )
    for candidate in (
        summary + [""] + tail + [note],
        summary + ["", note],
        summary,
    ):
        text = "\n".join(candidate).strip()
        if len(text) <= max_chars:
            return text

    # Not even the summary fits: hundreds of fields in a single call. Here
    # the cut is unavoidable, but `shape_lines` declares it at the top.
    return shape_lines("\n".join(summary), max_chars,
                       note="search fewer fields per call")


def register_report_tools(mcp):
    """Registers the Power BI report (PBIR) analysis tools on the MCP server."""
    register_tools(mcp, {"find_measure_usage_in_reports": find_measure_usage_in_reports})
