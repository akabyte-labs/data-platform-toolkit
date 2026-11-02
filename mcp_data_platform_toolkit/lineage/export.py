"""
A lineage graph as a file to keep, share or render elsewhere:

  - mermaid: a flowchart, one box per layer, that GitHub, Azure DevOps and
    most Markdown viewers draw;
  - markdown: a page with the diagram and the table of the edges;
  - csv: one row per edge, for a spreadsheet or another tool;
  - json: nodes and edges with all their attributes.

Edges found by a rule or still to verify are drawn dashed.
"""

from __future__ import annotations

import csv
import io
import json

from mcp_data_platform_toolkit.lineage.graph import Graph, to_payload

FORMATS = ("mermaid", "markdown", "csv", "json")


def _mermaid_text(text: str) -> str:
    return text.replace('"', "#quot;").replace("|", "/").replace("\n", " ")


def to_mermaid(graph: Graph) -> str:
    ids = {node_id: f"n{i}" for i, node_id in enumerate(sorted(graph.nodes, key=graph.node_order))}
    lines = ["flowchart LR"]
    by_layer: dict[str, list[str]] = {}
    for node_id in ids:
        by_layer.setdefault(graph.nodes[node_id].layer or "no layer", []).append(node_id)
    order = sorted(by_layer, key=lambda layer: graph.layer_rank("" if layer == "no layer" else layer))
    for index, layer in enumerate(order):
        lines.append(f'  subgraph L{index}["{_mermaid_text(layer)}"]')
        for node_id in by_layer[layer]:
            node = graph.nodes[node_id]
            tags = f" ({', '.join(node.flags)})" if node.flags else ""
            lines.append(f'    {ids[node_id]}["{_mermaid_text(node.label + tags)}"]')
        lines.append("  end")
    for edge in sorted(graph.edges, key=lambda e: (graph.node_order(e.source), graph.node_order(e.target))):
        if edge.source not in ids or edge.target not in ids:
            continue
        label = _mermaid_text(f"rule {edge.rule}" if edge.rule else edge.kind)
        arrow = "-.->" if edge.rule or edge.confidence != "certain" else "-->"
        lines.append(f"  {ids[edge.source]} {arrow}|{label}| {ids[edge.target]}")
    return "\n".join(lines) + "\n"


def to_csv(graph: Graph) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["source", "source_layer", "target", "target_layer", "kind", "confidence", "rule",
                     "evidence"])
    for edge in sorted(graph.edges, key=lambda e: (graph.node_order(e.source), graph.node_order(e.target))):
        source, target = graph.nodes.get(edge.source), graph.nodes.get(edge.target)
        writer.writerow([source.label if source else edge.source, source.layer if source else "",
                         target.label if target else edge.target, target.layer if target else "",
                         edge.kind, edge.confidence, edge.rule, edge.evidence])
    return buffer.getvalue()


def to_markdown(graph: Graph, title: str, header: list[str]) -> str:
    lines = [f"# {title}", ""]
    lines += [f"- {line}" for line in header]
    lines += ["", "```mermaid", to_mermaid(graph).rstrip(), "```", "", "| Source | Target | How | Evidence |",
              "|---|---|---|---|"]
    for edge in sorted(graph.edges, key=lambda e: (graph.node_order(e.source), graph.node_order(e.target))):
        source, target = graph.nodes.get(edge.source), graph.nodes.get(edge.target)

        def cell(text: str) -> str:
            return text.replace("|", "\\|")
        how = f"rule '{edge.rule}'" if edge.rule else edge.kind
        if edge.confidence not in ("certain", "rule"):
            how += f" ({edge.confidence})"
        lines.append(f"| {cell(source.label if source else edge.source)} "
                     f"| {cell(target.label if target else edge.target)} | {cell(how)} "
                     f"| {cell(edge.evidence)} |")
    return "\n".join(lines) + "\n"


def export(graph: Graph, export_format: str, title: str, header: list[str]) -> str:
    if export_format == "mermaid":
        return to_mermaid(graph)
    if export_format == "markdown":
        return to_markdown(graph, title, header)
    if export_format == "csv":
        return to_csv(graph)
    return json.dumps({"title": title, **to_payload(graph)}, indent=2, ensure_ascii=False) + "\n"
