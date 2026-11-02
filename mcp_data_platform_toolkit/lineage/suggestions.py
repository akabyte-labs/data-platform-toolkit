"""
Proposed rules for 'lineage.yaml', inferred from the names of the objects.

A table nothing loads in the SQL is usually loaded from an object whose name
it shares: a copy in another schema (`raw.orders` -> `clean.orders`), a
prefix or suffix added (`orders` -> `orders_history`, `v_orders` -> `orders`).
For each such table the object of the same or an earlier layer with the
longest common part of the name is taken as its probable source; pairs that
differ in the same way (schemas, prefix, suffix) become one rule, with the
common start of their names (digits generalized) in the pattern so it doesn't
match more than it should.

Every rule is then tried on the project, as the real configuration would be:
how many objects it matches, how many find their source, and whether it also
matches objects the SQL already loads. They are proposals, written for a
person to read and paste, never applied by themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from mcp_data_platform_toolkit.sql_project.config import Rule
from mcp_data_platform_toolkit.sql_project.project import DATA_KINDS

_MIN_CORE = 3


@dataclass
class Suggestion:
    name: str
    match: str
    source: str
    external: bool = False
    pairs: list = field(default_factory=list)           # (target label, source label)
    matched: list = field(default_factory=list)
    found: int = 0
    already_loaded: list = field(default_factory=list)  # matched objects the SQL loads
    alternatives: dict = field(default_factory=dict)    # target -> other candidate sources
    note: str = ""


def _common(a: str, b: str) -> tuple[int, int, int]:
    """
    (start in a, start in b, length) of the longest common substring; of
    several, the one that starts first in a, then in b. difflib does it
    several times faster than a table of lengths in Python, and a project
    with a thousand objects compares hundreds of targets with all of them.
    """
    start_a, start_b, length = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
        0, len(a), 0, len(b))
    return (start_a, start_b, length)


def _shape(text: str) -> str:
    return re.sub(r"\d", "#", text)


def _regex(shape: str) -> str:
    """A shape ('t##_') as a pattern ('t\\d{2}_')."""
    out, i = [], 0
    while i < len(shape):
        if shape[i] == "#":
            j = i
            while j < len(shape) and shape[j] == "#":
                j += 1
            out.append(rf"\d{{{j - i}}}")
            i = j
        else:
            out.append(re.escape(shape[i]))
            i += 1
    return "".join(out)


def _yaml_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def suggest_rules(lineage, targets: list[str] | None = None) -> dict:
    """
    {"rules": [Suggestion], "unmatched": [target labels], "targets": n,
    "handoffs": [labels]} for the tables of `targets` (by default
    `lineage.checks['loads_not_found']` and the objects that only read files,
    `lineage.checks['file_readers']`), plus a possible rule for the source
    systems named in the first layer. An object that only reads files is a
    target only when a similarly named object could have written them (a
    Parquet exported by a pipeline); otherwise people drop those files and
    there is nothing to explain: `handoffs` lists those that are targets.
    """
    config = lineage.config
    objects = lineage.analysis.objects
    data = [o for o in objects.values()
            if o.in_project and o.kind in DATA_KINDS and not config.is_ignored(o.schema)]
    by_label = {o.label: o for o in data}
    readers = lineage.checks["file_readers"]
    wanted = (lineage.checks["loads_not_found"] + list(readers) if targets is None else targets)
    targets = [by_label[label] for label in wanted if label in by_label]
    outgoing = lineage.objects.outgoing()

    # Every plausible source of every target, with the way the two names differ.
    target_keys = {t.key for t in targets}
    candidates: dict[str, list] = {}
    unmatched: list[str] = []
    for target in targets:
        layer = config.layer_index(config.layer_of(target.schema))
        found = []
        downstream = _downstream(outgoing, target.key)
        for source in data:
            # What reads the target can't be what loads it.
            if source is target or source.key in downstream:
                continue
            source_layer = config.layer_index(config.layer_of(source.schema))
            if layer is None or source_layer is None or source_layer > layer:
                continue
            t_name, s_name = target.name.lower(), source.name.lower()
            t_start, s_start, length = _common(t_name, s_name)
            if length < _MIN_CORE or length < 0.5 * max(len(t_name), len(s_name)):
                continue
            # The target's prefix and suffix with their digits generalized: 't01_customers'
            # and 't02_orders' differ in the same way from 'vw_customers' and 'vw_orders'.
            signature = (target.schema.lower(), _shape(t_name[:t_start]), _shape(t_name[t_start + length:]),
                         source.schema.lower(), s_name[:s_start], s_name[s_start + length:])
            rank = (source.key in target_keys, len(t_name) + len(s_name) - 2 * length,
                    layer - source_layer, source.label)
            found.append((length, rank, source, target.name[t_start:t_start + length], signature))
        if not found:
            # A file nobody writes may simply be dropped by people: not a missing load.
            if target.label not in readers:
                unmatched.append(target.label)
            continue
        longest = max(f[0] for f in found)
        candidates[target.label] = [f for f in found if f[0] >= 0.8 * longest]

    # The explanation that covers the most targets first: 'clean.<x> from raw.<x>' for
    # two tables beats 'clean.orders from curated.orders', which explains one, from a
    # table nothing loads either.
    by_target = {t.label: t for t in targets}
    remaining = set(candidates)
    pairs: dict[tuple, list] = {}
    alternatives: dict[str, list[str]] = {}
    while remaining:
        support: dict[tuple, list] = {}
        for label in remaining:
            for length, rank, source, core, signature in candidates[label]:
                support.setdefault(signature, []).append((label, length, rank, source, core))
        signature, members = min(support.items(), key=lambda item: (
            -len({m[0] for m in item[1]}), sum(m[2][0] for m in item[1]), sum(m[2][1] for m in item[1]),
            sum(m[2][2] for m in item[1]), item[0]))
        chosen: dict[str, tuple] = {}
        for label, _length, _rank, source, core in sorted(members, key=lambda m: (-m[1], m[2])):
            chosen.setdefault(label, (source, core))
        for label, (source, core) in chosen.items():
            pairs.setdefault(signature, []).append((by_target[label], source, core))
            alternatives[label] = [c[2].label for c in sorted(candidates[label], key=lambda c: (-c[0], c[1]))
                                   if c[2] is not source][:3]
        remaining -= set(chosen)

    suggestions = []
    names: set[str] = set()
    for signature, members in sorted(pairs.items(), key=lambda item: -len(item[1])):
        t_schema, t_prefix, t_suffix, s_schema, s_prefix, s_suffix = signature
        target_schema = members[0][0].schema
        source_schema = members[0][1].schema
        if len(members) == 1:
            target, source, _core = members[0]
            match = re.escape(f"{target.schema}.{target.name}")
            template = f"{source.schema}.{source.name}"
            note = "a single object: the rule names it"
        else:
            shapes = [_shape(core.lower()) for _t, _s, core in members]
            common = shapes[0]
            for shape in shapes[1:]:
                while not shape.startswith(common):
                    common = common[:-1]
            core_pattern = _regex(common) + r"\w*" if len(common) >= 2 else r"\w+"
            match = (f"{re.escape(target_schema)}\\.{_regex(t_prefix)}"
                     f"(?P<name>{core_pattern}){_regex(t_suffix)}")
            template = f"{source_schema}.{s_prefix}{{name}}{s_suffix}"
            note = ""
        base = f"{t_schema}-from-{s_schema}" if t_schema != s_schema else \
            f"{t_schema}-{(t_suffix or t_prefix).replace('#', '').strip('_') or 'copies'}"
        name, counter = base, 2
        while name in names:
            name, counter = f"{base}-{counter}", counter + 1
        names.add(name)
        suggestion = Suggestion(name, match, template, pairs=[(t.label, s.label) for t, s, _c in members],
                                note=note)
        suggestion.alternatives = {t.label: alternatives.get(t.label, []) for t, _s, _c in members
                                   if alternatives.get(t.label)}
        _try(suggestion, lineage, data)
        suggestions.append(suggestion)

    external = _source_systems(lineage, data)
    if external is not None:
        _try(external, lineage, data)
        suggestions.append(external)
    # Files read after the first layer that a similarly named object may have written.
    handoffs = [label for label in candidates if label in readers]
    dropped = sum(1 for t in targets if t.label in readers and t.label not in candidates)
    return {"rules": suggestions, "unmatched": unmatched, "targets": len(targets) - dropped,
            "handoffs": handoffs}


def _downstream(outgoing: dict, key: str) -> set[str]:
    """The objects that read `key`, directly or through others."""
    seen: set[str] = set()
    frontier = [key]
    while frontier:
        for edge in outgoing.get(frontier.pop(), []):
            if edge.target not in seen:
                seen.add(edge.target)
                frontier.append(edge.target)
    return seen


def _try(suggestion: Suggestion, lineage, data) -> None:
    """What the rule would do on the project."""
    rule = Rule(name=suggestion.name, match=re.compile(suggestion.match, re.IGNORECASE),
                source=suggestion.source, external=suggestion.external)
    incoming = lineage.objects.incoming()
    keys = {o.key for o in data}
    # An object that only reads files isn't loaded by the SQL: a pipeline wrote them.
    readers = set(lineage.checks["file_readers"])
    for obj in data:
        source = rule.source_for(obj.schema, obj.name)
        if source is None:
            continue
        suggestion.matched.append(obj.label)
        if suggestion.external or ".".join(p.strip("[]") for p in source.split(".")).lower() in keys:
            suggestion.found += 1
        if obj.label not in readers and any(not e.rule for e in incoming.get(obj.key, [])):
            suggestion.already_loaded.append(obj.label)


def _source_systems(lineage, data) -> Suggestion | None:
    """
    A rule for the source systems, when the first layer names its tables
    '<system>_<table>' with at least two systems of two tables each. Only a
    hint: the prefix might mean something else.
    """
    config = lineage.config
    if not config.layer_names or any(r.external for r in config.rules):
        return None
    first = config.layer_names[0]
    schemas = {o.schema for o in data if config.layer_of(o.schema) == first}
    for schema in sorted(schemas):
        names = [o.name for o in data if o.schema == schema]
        prefixes: dict[str, int] = {}
        for name in names:
            head, sep, rest = name.partition("_")
            if sep and rest and head.isalnum():
                prefixes[head.lower()] = prefixes.get(head.lower(), 0) + 1
        systems = {p: n for p, n in prefixes.items() if n >= 2}
        if len(systems) >= 2 and sum(systems.values()) >= 0.5 * len(names):
            listed = ", ".join(f"{p} ({n})" for p, n in sorted(systems.items(), key=lambda i: -i[1]))
            return Suggestion(
                f"{schema.lower()}-source-systems",
                f"{re.escape(schema)}\\.(?P<system>[a-z0-9]+)_(?P<table>\\w+)", "{system}.{table}",
                external=True,
                note=f"possible: the {first} tables of '{schema}' look named after their source "
                     f"system ({listed}); keep it only if the prefix really is the system")
    return None


def format_suggestions(result: dict, header: list[str]) -> str:
    lines = list(header)
    rules = result["rules"]
    if not result["targets"] and not rules:
        lines += ["", "Every table after the first layer is loaded by the SQL or by a rule: "
                      "nothing to suggest."]
        return "\n".join(lines)
    lines.append(f"Tables nothing loads: {result['targets']}. Proposed rules: {len(rules)}.")
    if rules:
        lines += ["", "To paste under 'rules:' in lineage.yaml (check each one first):", "", "rules:"]
        for rule in rules:
            lines.append(f"  - name: {rule.name}")
            lines.append(f"    match: {_yaml_quote(rule.match)}")
            lines.append(f"    from: {_yaml_quote(rule.source)}")
            if rule.external:
                lines.append("    external: true")
        lines += ["", "What each rule would do on the project:"]
        for rule in rules:
            count = len(rule.matched)
            lines.append(f"  {rule.name}: matches {count} object{'' if count == 1 else 's'}, "
                         f"{rule.found} with their source" + (f" ({rule.note})" if rule.note else ""))
            if rule.pairs:
                examples = ", ".join(f"{t} <- {s}" for t, s in rule.pairs[:5])
                more = f" ... and {len(rule.pairs) - 5} more" if len(rule.pairs) > 5 else ""
                lines.append(f"    inferred from: {examples}{more}")
            if rule.already_loaded:
                lines.append(f"    [!] it also matches objects the SQL already loads: "
                             f"{', '.join(rule.already_loaded[:8])}: make 'match' more precise")
            missing = len(rule.matched) - rule.found
            if missing and not rule.external:
                lines.append(f"    [!] {missing} matched objects would not find their source")
            for target, others in list(rule.alternatives.items())[:3]:
                lines.append(f"    other possible sources of {target}: {', '.join(others)}")
    if result["unmatched"]:
        lines += ["", "No object with a similar name for (describe their load by hand): "
                  + ", ".join(result["unmatched"][:20])]
    lines += ["", "After adding them, run test_lineage_config."]
    return "\n".join(lines)
