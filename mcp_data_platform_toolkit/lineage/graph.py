"""
A lineage graph: nodes, edges with their evidence, navigation upstream and
downstream, and the rendering within the output budget.

Rendering follows the rule of the other lineage tools: when the full form
doesn't fit, the DETAIL goes first (evidence, layers, the edges themselves)
and the name of every node stays as long as the names alone fit. A response
that had to be reduced says so, and says what was given up.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

CONFIDENCE_CERTAIN = "certain"      # read from the code (SQL, TMDL, PBIR)
CONFIDENCE_RULE = "rule"            # derived from a pattern of the configuration


@dataclass
class Node:
    id: str
    label: str
    layer: str = ""
    kind: str = ""
    flags: list[str] = field(default_factory=list)      # 'not in project', 'technical', ...
    where: str = ""                                     # file:line of the definition


@dataclass
class GraphEdge:
    source: str
    target: str
    kind: str
    evidence: str = ""
    confidence: str = CONFIDENCE_CERTAIN
    rule: str = ""


@dataclass
class Graph:
    nodes: dict[str, Node]
    edges: list[GraphEdge]
    layers: list[str] = field(default_factory=list)     # flow order, for sorting

    def layer_rank(self, layer: str) -> int:
        if layer == "source":
            return -1
        return self.layers.index(layer) if layer in self.layers else len(self.layers)

    def node_order(self, node_id: str) -> tuple:
        node = self.nodes.get(node_id)
        if node is None:
            return (len(self.layers) + 1, node_id)
        return (self.layer_rank(node.layer), node.label.lower())

    def incoming(self) -> dict[str, list[GraphEdge]]:
        result: dict[str, list[GraphEdge]] = {}
        for edge in self.edges:
            result.setdefault(edge.target, []).append(edge)
        return result

    def outgoing(self) -> dict[str, list[GraphEdge]]:
        result: dict[str, list[GraphEdge]] = {}
        for edge in self.edges:
            result.setdefault(edge.source, []).append(edge)
        return result

    def select(self, roots: list[str], direction: str = "both", depth: int = 0) -> "Graph":
        """
        The roots and what is connected to them upstream, downstream or both,
        up to `depth` steps (0 = no limit). Only the edges walked in the
        chosen direction are kept: upstream of a view are its sources, not
        the other consumers of those sources.
        """
        kept_nodes = set(roots)
        kept_edges: list[GraphEdge] = []
        walks = []
        if direction in ("upstream", "both"):
            walks.append((self.incoming(), "source"))
        if direction in ("downstream", "both"):
            walks.append((self.outgoing(), "target"))
        for adjacency, far_end in walks:
            seen = set(roots)
            queue = deque((root, 0) for root in roots)
            while queue:
                node, level = queue.popleft()
                if depth and level >= depth:
                    continue
                for edge in adjacency.get(node, []):
                    kept_edges.append(edge)
                    other = getattr(edge, far_end)
                    kept_nodes.add(other)
                    if other not in seen:
                        seen.add(other)
                        queue.append((other, level + 1))
        unique = list({id(e): e for e in kept_edges}.values())
        return Graph({k: v for k, v in self.nodes.items() if k in kept_nodes}, unique, self.layers)


def project(graph: Graph, owner: dict[str, str], nodes: dict[str, Node]) -> Graph:
    """
    The graph one level up: every node replaced by its owner (a column by its
    table, a visual by its page), the edges between two owners kept once per
    kind. `nodes` holds the owners' nodes; a node without an owner stays.
    """
    kept: dict[tuple, GraphEdge] = {}
    used: dict[str, Node] = {}
    for edge in graph.edges:
        source, target = owner.get(edge.source, edge.source), owner.get(edge.target, edge.target)
        if source == target:
            continue
        for end in (source, target):
            node = nodes.get(end) or graph.nodes.get(end)
            if node is not None:
                used[end] = node
        kept.setdefault((source, target, edge.kind),
                        GraphEdge(source, target, edge.kind, edge.evidence, edge.confidence, edge.rule))
    for node_id, node in nodes.items():
        used.setdefault(node_id, node)
    return Graph(used, list(kept.values()), graph.layers)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _node_text(graph: Graph, node_id: str, with_layer: bool = True) -> str:
    node = graph.nodes.get(node_id)
    if node is None:
        return node_id
    tags = ([node.layer] if with_layer and node.layer else []) + node.flags
    return f"{node.label} [{', '.join(tags)}]" if tags else node.label


def _how(edge: GraphEdge) -> str:
    return f"rule '{edge.rule}'" if edge.rule else edge.kind


def _sorted_edges(graph: Graph) -> list[GraphEdge]:
    return sorted(graph.edges, key=lambda e: (graph.node_order(e.source), graph.node_order(e.target),
                                              e.kind))


def _edge_lines(graph: Graph, evidence: bool) -> list[str]:
    lines = []
    for edge in _sorted_edges(graph):
        line = f"{_node_text(graph, edge.source)} -> {_node_text(graph, edge.target)}  {_how(edge)}"
        if evidence and edge.evidence and not edge.rule:
            line += f"  {edge.evidence}"
        lines.append(line)
    return lines


def _grouped_lines(graph: Graph) -> list[str]:
    """One line per target with all its sources: the names stay, layers and evidence go."""
    sources: dict[str, list[str]] = {}
    for edge in _sorted_edges(graph):
        mark = " (rule)" if edge.rule else ""
        sources.setdefault(edge.target, []).append(graph.nodes[edge.source].label + mark
                                                   if edge.source in graph.nodes else edge.source)
    return [f"{graph.nodes[t].label if t in graph.nodes else t} <- {', '.join(names)}"
            for t, names in sorted(sources.items(), key=lambda item: graph.node_order(item[0]))]


def _inventory_lines(graph: Graph) -> list[str]:
    by_layer: dict[str, list[str]] = {}
    for node in graph.nodes.values():
        by_layer.setdefault(node.layer or "unassigned", []).append(node.label)
    order = sorted(by_layer, key=lambda layer: graph.layer_rank("" if layer == "unassigned" else layer))
    return [f"{layer} ({len(by_layer[layer])}): {', '.join(sorted(by_layer[layer], key=str.lower))}"
            for layer in order]


def layer_counts(graph: Graph) -> str:
    counts: dict[str, int] = {}
    for node in graph.nodes.values():
        counts[node.layer or "unassigned"] = counts.get(node.layer or "unassigned", 0) + 1
    order = sorted(counts, key=lambda layer: graph.layer_rank("" if layer == "unassigned" else layer))
    return ", ".join(f"{layer} {counts[layer]}" for layer in order)


def format_edges(graph: Graph, header: list[str], max_chars: int | None = None,
                 how_to_narrow: str = "") -> str:
    """
    One line per edge, in flow order, in the richest form that fits.

    Rungs: full (layers and file:line) -> without evidence -> one line per
    target with its sources -> the names of the nodes by layer, without
    edges -> counts by layer.
    """
    if not graph.edges and not graph.nodes:
        return "\n".join(header + ["", "Nothing found for the selected criteria."])
    count = f"{len(graph.nodes)} nodes, {len(graph.edges)} edges"
    ladder = [
        ("", lambda: _edge_lines(graph, evidence=True)),
        ("without the file and line of each edge", lambda: _edge_lines(graph, evidence=False)),
        ("one line per target with its sources, without layers",
         lambda: _grouped_lines(graph)),
        ("no edges: only the names of the nodes, by layer", lambda: _inventory_lines(graph)),
        ("only the number of nodes by layer", lambda: [layer_counts(graph)]),
    ]
    for loss, build in ladder:
        body = "\n".join(header + [""] + build())
        if not loss:
            if max_chars is None or len(body) <= max_chars:
                return body
            continue
        names_kept = not loss.startswith("only the number")
        guarantee = ("No node was omitted: they are all here, with less detail."
                     if names_kept else "WARNING: at this level the NAMES of the nodes are not listed.")
        note = (f"\n[response reduced ({loss}): {count}. {guarantee}"
                + (f" {how_to_narrow}" if how_to_narrow else "") + "]")
        if max_chars is None or len(body) + len(note) <= max_chars:
            return body + note
    return "\n".join(header + ["", f"{count}: not even the counts fit the budget. "
                               + (how_to_narrow or "Narrow the request.")])


def format_tree(graph: Graph, roots: list[str], direction: str, header: list[str],
                max_chars: int | None = None, how_to_narrow: str = "", max_depth: int = 30) -> str:
    """
    The roots with what is upstream ('<-') and/or downstream ('->') of them,
    indented. Every node is expanded once: a node reached again is marked
    instead of reprinting its whole subtree, which on graphs with diamonds
    grew exponentially. Above the budget the edge view is used: a tree cut in
    half looks complete and isn't.
    """
    lines: list[str] = []
    walks = []
    if direction in ("upstream", "both"):
        walks.append(("upstream", graph.incoming(), "source", "<-"))
    if direction in ("downstream", "both"):
        walks.append(("downstream", graph.outgoing(), "target", "->"))
    for name, adjacency, far_end, arrow in walks:
        if len(walks) > 1:
            lines.append(f"-- {name} --")
        expanded: set[str] = set()
        for root in sorted(roots, key=graph.node_order):
            lines.append(_node_text(graph, root))
            _subtree(graph, root, adjacency, far_end, arrow, 1, expanded, lines, max_depth)
        lines.append("")
    body = "\n".join(header + [""] + lines).rstrip()
    if max_chars is None or len(body) <= max_chars:
        return body
    return format_edges(graph, header + ["(tree too large for the budget: one line per edge)"],
                        max_chars, how_to_narrow)


def _subtree(graph, node, adjacency, far_end, arrow, level, expanded, lines, max_depth) -> None:
    if node in expanded:
        return
    expanded.add(node)
    edges = sorted(adjacency.get(node, []), key=lambda e: graph.node_order(getattr(e, far_end)))
    for edge in edges:
        other = getattr(edge, far_end)
        evidence = f"  {edge.evidence}" if edge.evidence and not edge.rule else ""
        again = other in expanded and adjacency.get(other)
        line = f"{'   ' * level}{arrow} {_node_text(graph, other)}  {_how(edge)}{evidence}"
        if again:
            line += "  (expanded above)"
        lines.append(line)
        if level >= max_depth:
            if adjacency.get(other):
                lines.append(f"{'   ' * (level + 1)}... (depth limit)")
            continue
        _subtree(graph, other, adjacency, far_end, arrow, level + 1, expanded, lines, max_depth)


def to_payload(graph: Graph) -> dict:
    """The graph as plain data, for the JSON output."""
    return {
        "nodes": [
            {"id": n.id, "label": n.label, "layer": n.layer, "kind": n.kind,
             **({"flags": n.flags} if n.flags else {}), **({"where": n.where} if n.where else {})}
            for n in sorted(graph.nodes.values(), key=lambda n: graph.node_order(n.id))
        ],
        "edges": [
            {"source": e.source, "target": e.target, "kind": e.kind, "confidence": e.confidence,
             **({"rule": e.rule} if e.rule else {}), **({"evidence": e.evidence} if e.evidence else {})}
            for e in _sorted_edges(graph)
        ],
    }
