"""
The reports in the end-to-end lineage: every visual, filter pane,
drillthrough or tooltip binding, bookmark and report-level measure of the
PBIR reports bound to the model, with an edge from each model field it
really binds.

Only bindings count. A name that appears only in a title or a queryRef is a
probable usage for `find_measure_usage_in_reports`, not a data dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcp_data_platform_toolkit.lineage.graph import GraphEdge, Node
from mcp_data_platform_toolkit.lineage.model_lineage import column_id, measure_id
from mcp_data_platform_toolkit.reports import report_tools as reports
from mcp_data_platform_toolkit.reports.pbir_references import collect_pbir_references, dax_reference_index

REPORT_LAYER = "report"


@dataclass
class ReportPart:
    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    owners: dict = field(default_factory=dict)          # node id -> page / report node (object level)
    analyzed: list = field(default_factory=list)        # report names
    not_analyzed: list = field(default_factory=list)    # "name: why"
    unreadable: list = field(default_factory=list)
    by_column: dict = field(default_factory=dict)       # column name (lower) -> model column nodes

    @property
    def missing(self) -> list:
        """
        The reports whose use is missing from the lineage: the PBIR-Legacy ones and the
        folders with no definition. A report bound to another model is not missing, it
        belongs to another lineage.
        """
        return [entry for entry in self.not_analyzed if ": bound to the model '" not in entry]


def _visual_title(data) -> str:
    """The literal title of a visual, '' when it has none or it is computed."""
    try:
        title = data["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"]
        value = title["expr"]["Literal"]["Value"]
    except (KeyError, IndexError, TypeError):
        return ""
    text = str(value)
    return text[1:-1].replace("''", "'") if len(text) >= 2 and text[0] == text[-1] == "'" else text


def build_report_part(reports_path: str, model_folder: str, model) -> ReportPart:
    """
    `model` is the `ModelPart` of the semantic model: its nodes are what the
    report bindings point to.
    """
    part = ReportPart()
    for node_id in model.nodes:
        if node_id.startswith("model:"):
            part.by_column.setdefault(node_id.split("[", 1)[1][:-1], []).append(node_id)
    try:
        folders = reports.find_report_folders(reports_path)
    except (FileNotFoundError, ValueError):
        return part
    name = reports._model_name_from_folder(model_folder)
    selection = reports._select_reports(folders, {"folder": model_folder, "name": name})
    part.not_analyzed.extend(f"{n}: PBIR-Legacy format, not analyzed" for n in selection["legacy"])
    part.not_analyzed.extend(f"{n}: no report definition found" for n in selection["unknown_format"])
    part.not_analyzed.extend(f"{n}: bound to the model '{m}'" for n, m in selection["other_model"])

    for report_folder, _binding in selection["analyzed"]:
        report = reports._report_name(report_folder)
        part.analyzed.append(report)
        report_node = f"report:{report.lower()}"
        part.nodes[report_node] = Node(report_node, report, REPORT_LAYER, "report")
        report_measures: dict[str, str] = {}

        artifacts = list(reports._iter_report_artifacts(report_folder))
        # Report-level measures first: visuals bind them like model measures.
        for artifact, path in artifacts:
            if not artifact.get("dax"):
                continue
            try:
                data = reports._read_json(path)
            except reports.PER_FILE_ERRORS as exc:
                part.unreadable.append(f"{artifact['context']}: {type(exc).__name__}")
                continue
            for entity in (data.get("entities") or []) if isinstance(data, dict) else []:
                for measure in (entity.get("measures") or []) if isinstance(entity, dict) else []:
                    if not isinstance(measure, dict) or not isinstance(measure.get("expression"), str):
                        continue
                    measure_name = str(measure.get("name") or "?")
                    node = f"reportmeasure:{report.lower()}|{measure_name.lower()}"
                    part.nodes[node] = Node(node, f"{report} / [{measure_name}]", REPORT_LAYER,
                                            "report measure")
                    part.owners[node] = report_node
                    report_measures[measure_name.lower()] = node
                    _bind(part, model, dax_reference_index(measure["expression"]), node, report_measures,
                          "report measure", f"{report} (reportExtensions.json)")

        for artifact, path in artifacts:
            if artifact.get("dax"):
                continue
            try:
                data = reports._read_json(path)
            except reports.PER_FILE_ERRORS as exc:
                part.unreadable.append(f"{artifact['context']}: {type(exc).__name__}")
                continue
            scope = artifact.get("scope")
            scoped = data.get(scope) if scope and isinstance(data, dict) else data
            if not scoped:
                continue
            node, label, kind = _artifact_node(report, artifact, data)
            page_node = report_node if not artifact["page"] else \
                f"page:{report.lower()}|{artifact['page'].lower()}"
            if page_node not in part.nodes:
                part.nodes[page_node] = Node(page_node, f"{report} / {artifact['page']}", REPORT_LAYER, "page")
                part.owners[page_node] = report_node
            if node not in part.nodes:
                part.nodes[node] = Node(node, label, REPORT_LAYER, kind)
                part.owners[node] = page_node
            _bind(part, model, collect_pbir_references(scoped), node, report_measures, "binding",
                  artifact["context"])
    return part


def _artifact_node(report: str, artifact: dict, data) -> tuple[str, str, str]:
    page = artifact["page"]
    kind = artifact["kind"]
    base = f"{report} / {page}" if page else report
    if kind == "visual":
        visual = data.get("visual") if isinstance(data, dict) else None
        visual_type = visual.get("visualType") if isinstance(visual, dict) else None
        title = _visual_title(data)
        name = f"'{title}' ({visual_type or 'visual'})" if title else \
            f"{visual_type or 'visual'} {artifact['visual_id']}"
        return (f"visual:{report.lower()}|{page.lower()}|{artifact['visual_id'].lower()}",
                f"{base} / {name}", "visual")
    if kind == "bookmark":
        label = artifact["context"].split("|")[-1].strip().strip("()")
        return f"bookmark:{report.lower()}|{label.lower()}", f"{report} / {label}", "bookmark"
    scope = artifact.get("scope") or ""
    what = "drillthrough/tooltip fields" if scope == "pageBinding" else "filters"
    return (f"filter:{report.lower()}|{page.lower()}|{what}", f"{base} / ({what})", "filter")


def _bind(part: ReportPart, model, index: dict, target: str, report_measures: dict, kind: str,
          evidence: str) -> None:
    """Edges from the model fields of a reference index to `target`."""
    nodes = model.nodes
    by_column = part.by_column
    sources: set[str] = set()
    for name in index["measures"]:
        if name in report_measures and report_measures[name] != target:
            sources.add(report_measures[name])
        elif measure_id(name) in nodes:
            sources.add(measure_id(name))
    for table, column in index["columns"]:
        node = column_id(table, column) if table else None
        if node and node in nodes:
            sources.add(node)
        elif not table and len(by_column.get(column, [])) == 1:
            sources.add(by_column[column][0])
        elif table and measure_id(column) in nodes and model.measure_tables.get(column) is not None:
            sources.add(measure_id(column))         # a measure written as Table[Measure]
    for table, hierarchy, level in index.get("levels", ()):
        column = model.levels.get((table, hierarchy, level))
        if column is not None and column_id(table, column) in nodes:
            sources.add(column_id(table, column))
    for source in sorted(sources):
        part.edges.append(GraphEdge(source, target, kind, evidence))
