"""
A first 'lineage.yaml' for a database project, drafted from its SQL, and the
questions that complete it.

Without any configuration the SQL already says most of what the file holds:
which schemas data flows between and in which order, the schemas no data
flows through, the tables nothing loads, the columns no source fills. Names
add the rest: a schema called 'raw' or 'staging' is where data arrives,
'mart' where it is consumed, 'audit' holds no data flow. With the Power BI
project, the schemas the model reads are the last layer.

What stays uncertain becomes a question, with the proposed answer first: a
schema whose name says nothing or contradicts its flows, a rule inferred
from the object names, a table nothing loads and no name explains, the
columns that look created by the ETL. The draft is the file as it would be
with the proposed answers; drafting again with the user's answers applies
them and leaves only what is still open. Nothing is written here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from mcp_data_platform_toolkit.lineage.draft_notice import DRAFT_LINES, DRAFT_MARKER
from mcp_data_platform_toolkit.lineage.suggestions import suggest_rules
from mcp_data_platform_toolkit.sql_project.config import (DEFAULT_SCHEMA, SOURCE_LAYER, ConfigError, LineageConfig, Rule,
                                find_project_config, parse_config)
from mcp_data_platform_toolkit.sql_project.project import DATA_KINDS, load_unconfigured_lineage, lookup_pairs, with_layers
from mcp_data_platform_toolkit.sql_project.sql_files import find_database_project, project_default_schema

STANDARD_LAYERS = ("bronze", "silver", "gold")
MAX_OUTSIDE_SHOWN = 10
IGNORE = "ignore"

# Words of schema names and the layer they point to. A strong word is enough
# when the flows agree; a weak one is a guess for the user to confirm.
_STRONG_WORDS = {
    "bronze": {"bronze", "raw", "landing", "staging", "stg", "stage", "ingest", "ingestion"},
    "silver": {"silver", "clean", "cleansed", "curated", "conformed", "refined", "enriched",
               "trusted"},
    "gold": {"gold", "mart", "marts", "datamart", "presentation", "reporting", "consumption",
             "serving"},
}
_WEAK_WORDS = {
    "bronze": {"src", "source", "import", "ext", "extract", "lnd", "land"},
    # 'integration' is the integration layer of a Data Vault, but also where
    # staging lands (WideWorldImportersDW): a guess to confirm, never a certainty.
    "silver": {"core", "dwh", "dw", "edw", "int", "integration", "base", "vault", "rdv", "bdv",
               "transform"},
    # A star schema is where the data is consumed.
    "gold": {"dm", "pbi", "bi", "semantic", "rpt", "report", "analytics", "kpi", "pres", "publish",
             "dimension", "dimensions", "dim", "fact", "facts", "star"},
}
# Configuration, logging, tests, copies: no data flows through them.
_IGNORE_WORDS = {"config", "cfg", "conf", "configuration", "audit", "log", "logs", "logging", "meta",
                 "metadata", "ctl", "control", "util", "utils", "utility", "admin", "security",
                 "sec", "test", "tests", "tst", "uat", "monitor", "monitoring", "tsqlt", "archive",
                 "backup", "bak", "deprecated", "old", "sandbox"}

# Words of the column names an ETL creates: load dates, run ids, surrogate keys, hashes.
_TECH_PREFIXES = {"etl", "dw", "dwh", "meta", "audit", "tech", "sys"}
_TECH_SUFFIXES = {"sk", "hk", "hash", "hashkey", "hashdiff"}
_LOAD_WORDS = {"load", "loaded", "insert", "inserted", "update", "updated", "modified", "created",
               "ingest", "ingested", "processed", "extract", "extracted", "refresh", "refreshed"}
_TIME_WORDS = {"date", "time", "datetime", "ts", "timestamp", "at", "on", "dt"}
_RUN_WORDS = {"batch", "run", "job", "pipeline", "process", "execution", "load"}
_RUN_ID_WORDS = {"id", "key", "nr", "no", "number"}
_TECH_PAIRS = {("valid", "from"), ("valid", "to"), ("valid", "until"), ("is", "current"),
               ("is", "deleted"), ("record", "source"), ("row", "version"), ("current", "flag"),
               ("deleted", "flag")}
# A column without source in this many tables is generated, whatever its name.
_TECH_MIN_TABLES = 3

# Load questions shown at once: the others wait for the next round.
MAX_LOAD_QUESTIONS = 10

_YES = {"yes", "y", "true", "keep", "ok", "all"}
_NO = {"no", "n", "false", "drop", "remove", "none"}
_NONE = {"none", "no", "skip", "-", "unknown"}
_YAML_WORDS = {"y", "n", "yes", "no", "on", "off", "true", "false", "null"}


@dataclass
class SchemaInfo:
    name: str
    objects: dict = field(default_factory=dict)         # kind -> count
    upstream: set = field(default_factory=set)          # other schemas it reads (lowercase)
    downstream: set = field(default_factory=set)        # other schemas reading it
    outside: bool = False                               # reads files or objects outside the project
    internal: bool = False                              # data flows between its own objects
    lookups: set = field(default_factory=set)           # schemas it reads only for lookups
    named: dict = field(default_factory=dict)           # layer -> its objects whose prefix says it
    examples: dict = field(default_factory=dict)        # layer -> a few of those objects
    model_reads: int = 0                                # its objects the Power BI model reads
    rank: int = 0                                       # steps from the start of the flow
    layer: str = ""                                     # a layer, or IGNORE
    why: str = ""
    sure: bool = False                                  # no question to ask
    answered: bool = False

    @property
    def flows(self) -> bool:
        return bool(self.upstream or self.downstream or self.lookups or self.outside or self.model_reads)


@dataclass
class DraftRule:
    name: str
    match: str
    source: str
    external: bool = False


@dataclass
class Question:
    key: str
    text: str
    proposed: str
    otherwise: str
    details: list = field(default_factory=list)


@dataclass
class Draft:
    project: object
    existing: str                                       # an existing configuration file, or ''
    default_schema: str
    default_from: str
    model: str                                          # the Power BI model read, or ''
    schemas: list                                       # SchemaInfo, in flow order
    layers: list                                        # [(layer, [schema, ...])], in flow order
    ignore: list
    rules: list                                         # DraftRule
    technical: list                                     # patterns
    questions: list
    applied: int
    not_applied: list                                   # 'key: why'
    targets: list                                       # tables nothing loads in the SQL
    unexplained: list                                   # ... and no rule of the draft loads
    text: str = ""
    outside: list = field(default_factory=list)         # read by the SQL, defined outside the project


# ---------------------------------------------------------------------------
# The draft
# ---------------------------------------------------------------------------

def draft_config(sql_project_path: str, answers=None, project_path: str = "") -> Draft:
    """The draft of the configuration with `answers` applied, and the questions still open."""
    given = _answers(answers)
    used: set[str] = set()
    not_applied: list[str] = []
    questions: list[Question] = []

    project = find_database_project(sql_project_path)
    default_schema, default_from = project_default_schema(project.sqlproj), "the .sqlproj"
    if not default_schema:
        default_schema, default_from = DEFAULT_SCHEMA, "default"
    if "default_schema" in given:
        value = _unbracket(_text(given["default_schema"]))
        if value and "." not in value:
            default_schema, default_from = value, "answer"
            used.add("default_schema")
        else:
            not_applied.append("default_schema: must be a schema name")
    base = load_unconfigured_lineage(sql_project_path, default_schema)

    model, model_reads = "", {}
    if project_path and str(project_path).strip():
        model, model_reads = _model_reads(project_path, base)
    schemas = _schemas(base, model_reads)
    if not schemas:
        raise LookupError(f"The database project '{project.name}' defines no table, view or function: "
                          f"there is nothing to configure.")
    _rank(schemas)
    _place(schemas, given, used, not_applied)
    for key, info in sorted(schemas.items(), key=lambda item: (item[1].rank, item[0])):
        if not info.sure:
            questions.append(_schema_question(key, info, schemas))
    layers = _layer_order(schemas, given, used, not_applied, questions)
    ignore = sorted((i.name for i in schemas.values() if i.layer == IGNORE), key=str.lower)
    config = LineageConfig(path="", layers=tuple((name, tuple(members)) for name, members in layers),
                           ignore=tuple(ignore), default_schema=default_schema)
    lineage = with_layers(base, config)
    objects = _data_objects(lineage)

    readers = lineage.checks["file_readers"]
    manual = _manual_loads(given, objects, used, not_applied)
    rules = [rule for rule in manual.values() if rule is not None]
    candidates = list(lineage.checks["loads_not_found"]) + list(readers)
    suggested = suggest_rules(lineage, [t for t in candidates if t.lower() not in manual])
    # A file reader is a table to explain only when a similarly named object may have written its files.
    handoffs = {label: readers[label] for label in suggested["handoffs"]}
    targets = list(lineage.checks["loads_not_found"]) + list(handoffs)
    for suggestion in suggested["rules"]:
        if _decide_rule(suggestion, given, used, not_applied, questions):
            rules.append(DraftRule(suggestion.name, suggestion.match, suggestion.source,
                                   suggestion.external))
    unexplained = _unexplained(objects, targets, rules)
    for label in unexplained:
        if label.lower() not in manual:
            questions.append(_load_question(objects[label.lower()], handoffs.get(label, [])))

    technical = _technical(lineage, given, used, not_applied, questions)

    ordered = sorted(schemas.values(), key=lambda i: (_layer_position(i.layer, layers), i.rank,
                                                      i.name.lower()))
    for key in given:
        if key not in used and not any(entry.startswith(f"{key}:") for entry in not_applied):
            not_applied.append(f"{key}: {_unused_reason(key)}")
    draft = Draft(project, find_project_config(project.folder, project.sqlproj), default_schema, default_from, model,
                  ordered, layers, ignore, rules, technical, questions, len(used), not_applied,
                  targets, unexplained)
    draft.outside = sorted((o.label for o in lineage.analysis.objects.values()
                            if not o.in_project and o.kind == "unknown"), key=str.lower)
    draft.text = render_yaml(draft)
    try:
        parse_config(draft.text, "lineage.yaml (draft)")
    except ConfigError as exc:                          # an answer the checks above let through
        draft.not_applied.extend(f"the draft: {p}" for p in exc.problems)
    return draft


def _unused_reason(key: str) -> str:
    kind = key.split(".", 1)[0]
    reasons = {
        "schema": "no table, view or function of the project is in this schema",
        "rule": "no rule of this draft has this name (the rules change with the layers: see the "
                "open questions)",
        "load": "not a table of the project",
    }
    return reasons.get(kind, "unknown key (keys: schema.<schema>, layer_order, default_schema, "
                             "rule.<name>, load.<schema.table>, technical_columns)")


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

def _answers(answers) -> dict:
    """{normalized key: value}: 'Schema.[Raw]' and 'schema.raw' are the same key."""
    if answers is None or answers == "":
        return {}
    if isinstance(answers, str):
        try:
            answers = json.loads(answers)
        except ValueError:
            answers = None
    if not isinstance(answers, dict):
        raise ValueError("'answers' must be an object of key: answer, e.g. "
                         "{\"schema.core\": \"silver\", \"rule.clean-from-raw\": \"no\"}.")
    result = {}
    for key, value in answers.items():
        kind, _dot, rest = str(key).strip().partition(".")
        kind = kind.lower()
        if kind in ("schema", "load") and rest:
            result[f"{kind}.{_normalize(rest)}"] = value
        elif kind == "rule" and rest:
            result[f"rule.{rest.strip().lower()}"] = value
        else:
            result[str(key).strip().lower()] = value
    return result


def _text(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value if value is not None else "").strip()


def _yes(value) -> bool | None:
    text = _text(value).lower()
    return True if text in _YES else False if text in _NO else None


def _unbracket(name: str) -> str:
    text = name.strip()
    if len(text) >= 2 and text[0] in "[\"" and text[-1] in "]\"":
        return text[1:-1]
    return text


def _normalize(name: str) -> str:
    return ".".join(_unbracket(part).lower() for part in str(name).strip().split("."))


# ---------------------------------------------------------------------------
# Schemas and their layers
# ---------------------------------------------------------------------------

def _words(name: str) -> list[str]:
    """'RawData' -> ['raw', 'data'], 'stg_2' -> ['stg', '2'], 'etl_loaded_at' -> ['etl', 'loaded', 'at']."""
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)]


def _word_layer(name: str) -> tuple[str, bool]:
    """(layer or IGNORE or '', strong) from the words of a schema name; '' when they disagree."""
    words = set(_words(name))
    found = []
    for strong, table in ((True, _STRONG_WORDS), (False, _WEAK_WORDS)):
        found += [(layer, strong) for layer, known in table.items() if words & known]
    if words & _IGNORE_WORDS:
        found.append((IGNORE, True))
    if not found or len({layer for layer, _strong in found}) > 1:
        return "", False
    return found[0][0], any(strong for _layer, strong in found)


def _model_reads(project_path: str, lineage) -> tuple[str, dict]:
    """(model name, {schema: objects of it the model reads})."""
    import os

    from mcp_data_platform_toolkit.lineage.model_lineage import build_model_part
    from mcp_data_platform_toolkit.semantic_model.project_paths import find_semantic_model_folder
    part = build_model_part(project_path, lineage)
    objects = lineage.analysis.objects
    counts: dict[str, set] = {}
    for read in part.reads.values():
        for key in read:
            obj = objects.get(key)
            if obj is not None and obj.in_project:
                counts.setdefault(obj.schema.lower(), set()).add(key)
    name = os.path.basename(find_semantic_model_folder(project_path))
    name = name[: -len(".SemanticModel")] if name.endswith(".SemanticModel") else name
    return name, {schema: len(keys) for schema, keys in counts.items()}


def _schemas(lineage, model_reads: dict) -> dict[str, SchemaInfo]:
    """The schemas holding data, with the flows between them (lowercase name -> SchemaInfo)."""
    objects = lineage.analysis.objects
    schemas: dict[str, SchemaInfo] = {}
    for obj in sorted(objects.values(), key=lambda o: o.key):
        if obj.in_project and obj.kind in DATA_KINDS:
            info = schemas.setdefault(obj.schema.lower(), SchemaInfo(obj.schema))
            info.objects[obj.kind] = info.objects.get(obj.kind, 0) + 1
            # The prefix of the name (DimCustomer, stg_orders): where a schema holds a whole warehouse.
            first = _words(obj.name)[:1]
            layer = _word_layer(first[0])[0] if first else ""
            if layer in STANDARD_LAYERS:
                info.named[layer] = info.named.get(layer, 0) + 1
                info.examples.setdefault(layer, [])
                if len(info.examples[layer]) < 2:
                    info.examples[layer].append(obj.name)
    between: set[tuple[str, str]] = set()
    for edge in lineage.objects.edges:
        source, target = objects.get(edge.source), objects.get(edge.target)
        if target is None or target.schema.lower() not in schemas or not target.in_project:
            continue
        reader = schemas[target.schema.lower()]
        if source is None or not source.in_project or source.kind not in DATA_KINDS:
            reader.outside = True
        elif source.schema.lower() != target.schema.lower():
            between.add((source.schema.lower(), target.schema.lower()))
        else:
            reader.internal = True
    # Flows both ways: the lighter direction is lookups, not where the data
    # comes from (the same judgement as test_lineage_config's).
    lookups = lookup_pairs(objects, lineage.objects.edges, lineage.columns.edges)
    for (source, target) in between:
        if (source, target) in lookups:
            schemas[target].lookups.add(source)
            continue
        schemas[target].upstream.add(source)
        schemas[source].downstream.add(target)
    for schema, count in model_reads.items():
        if schema in schemas:
            schemas[schema].model_reads = count
    return schemas


def _rank(schemas: dict[str, SchemaInfo]) -> None:
    """Steps from the start of the flow; a flow going backwards is cut where it closes the loop."""
    done: dict[str, int] = {}
    visiting: set[str] = set()

    def visit(name: str) -> int:
        if name in done:
            return done[name]
        if name in visiting:
            return 0
        visiting.add(name)
        rank = max((visit(u) + 1 for u in sorted(schemas[name].upstream)), default=0)
        visiting.discard(name)
        done[name] = rank
        return rank

    for name in sorted(schemas):
        schemas[name].rank = visit(name)


def _named_layer(info: SchemaInfo) -> str:
    """
    The layer the names of its objects agree on: most of its objects named like
    it (DimCustomer, FactSales: gold) and no other layer among them. '' otherwise.
    """
    total = sum(info.objects.values())
    if len(info.named) != 1 or not total:
        return ""
    layer, count = next(iter(info.named.items()))
    return layer if count * 2 > total else ""


def _place(schemas: dict[str, SchemaInfo], given: dict, used: set, not_applied: list) -> None:
    """
    The layer of every schema: the user's answer; else the name (and the
    model, which reads the last layer); else its place in the flows,
    between the layers of what it reads and of what reads it.
    """
    index: dict[str, int] = {}

    for key, info in schemas.items():
        if f"schema.{key}" not in given:
            continue
        value = _text(given[f"schema.{key}"]).lower()
        if value == SOURCE_LAYER:
            not_applied.append(f"schema.{key}: '{SOURCE_LAYER}' is reserved for the systems and files "
                               f"outside the project")
        elif not re.fullmatch(r"[a-z_][a-z0-9_-]*", value):
            not_applied.append(f"schema.{key}: '{value}' is not a layer name (letters, digits, '_', '-') "
                               f"nor '{IGNORE}'")
        else:
            info.layer, info.why, info.sure, info.answered = value, "answer", True, True
            used.add(f"schema.{key}")
            if value in STANDARD_LAYERS:
                index[key] = STANDARD_LAYERS.index(value)

    for key, info in schemas.items():
        if info.answered:
            continue
        word, strong = _word_layer(info.name)
        if word == IGNORE:
            info.layer, info.why, info.sure = IGNORE, "name", not info.flows
        elif word and not strong and word != STANDARD_LAYERS[0] and not info.upstream \
                and not info.outside and info.downstream and not info.model_reads:
            # A guess from the name against what the flows show: nothing in the SQL
            # loads it and it feeds the others, so data arrives here.
            info.layer, info.why, info.sure = STANDARD_LAYERS[0], f"flows, though its name suggests {word}", False
            index[key] = 0
        elif word:
            info.layer, info.why, info.sure = word, "name", strong
            index[key] = STANDARD_LAYERS.index(word)
            if info.model_reads and word != STANDARD_LAYERS[-1]:
                info.sure = False               # the model reads it: is it the last layer?
        elif info.model_reads:
            info.layer, info.why, info.sure = STANDARD_LAYERS[-1], "read by the model", not info.downstream
            index[key] = len(STANDARD_LAYERS) - 1
        elif _named_layer(info):
            info.layer, info.why, info.sure = _named_layer(info), "objects' names", False
            index[key] = STANDARD_LAYERS.index(info.layer)

    for key in sorted(schemas, key=lambda k: (schemas[k].rank, k)):
        info = schemas[key]
        if info.layer:
            continue
        if not info.flows and not info.internal:
            info.layer, info.why = IGNORE, "no data flow"
            continue
        ups = [index[u] for u in info.upstream if u in index]
        downs = [index[d] for d in info.downstream if d in index]
        if not info.upstream:
            position = 0                        # data arrives here
        elif not ups and not downs:
            position = min(info.rank, len(STANDARD_LAYERS) - 1)
        elif not downs:
            position = min(max(ups) + 1, len(STANDARD_LAYERS) - 1)
        elif not ups:
            position = max(min(downs) - 1, 0)
        elif min(downs) - max(ups) >= 2:
            position = max(ups) + 1
        else:
            position = max(ups)
        info.layer, info.why = STANDARD_LAYERS[position], "flows"
        index[key] = position

    # A name contradicted by the flows is a question after all.
    for key, info in schemas.items():
        if not info.sure or info.answered or key not in index:
            continue
        if any(index.get(u, -1) > index[key] for u in info.upstream) or \
                any(index.get(d, len(STANDARD_LAYERS)) < index[key] for d in info.downstream):
            info.sure = False
            info.why += ", but the flows disagree"


def _layer_order(schemas, given, used, not_applied, questions) -> list[tuple[str, list[str]]]:
    """[(layer, [schema, ...])] in flow order: the standard layers in theirs, new layers by their flows."""
    members: dict[str, list[str]] = {}
    ranks: dict[str, list[int]] = {}
    for info in sorted(schemas.values(), key=lambda i: (i.rank, i.name.lower())):
        if info.layer and info.layer != IGNORE:
            members.setdefault(info.layer, []).append(info.name)
            ranks.setdefault(info.layer, []).append(info.rank)

    def mean(layer: str) -> float:
        return sum(ranks[layer]) / len(ranks[layer])

    order = [layer for layer in STANDARD_LAYERS if layer in members]
    custom = sorted((layer for layer in members if layer not in STANDARD_LAYERS),
                    key=lambda layer: (mean(layer), layer))
    for layer in custom:
        position = 0
        for i, other in enumerate(order):
            if mean(other) <= mean(layer):
                position = i + 1
        order.insert(position, layer)

    if "layer_order" in given:
        value = given["layer_order"]
        listed = [str(v).strip().lower() for v in value] if isinstance(value, (list, tuple)) else \
            [p.strip().lower() for p in re.split(r"[,\s>]+", _text(value)) if p.strip()]
        if sorted(listed) == sorted(order):
            order = listed
            used.add("layer_order")
        else:
            not_applied.append(f"layer_order: must list each layer once, in flow order: {', '.join(order)}")
    if custom and "layer_order" not in used:
        questions.append(Question(
            "layer_order", "In which order does the data flow through the layers?", ", ".join(order),
            "the same layers in another order, the first one closest to the sources",
            [f"{layer}: {', '.join(members[layer])}" for layer in order]))
    return [(layer, members[layer]) for layer in order]


def _layer_position(layer: str, layers: list) -> int:
    names = [name for name, _members in layers]
    return names.index(layer) if layer in names else len(names)


def _objects_text(info: SchemaInfo) -> str:
    names = {"table": "table", "view": "view", "external_table": "external table", "function": "function"}
    parts = [f"{count} {names.get(kind, kind)}{'' if count == 1 else 's'}"
             for kind, count in sorted(info.objects.items(), key=lambda item: -item[1])]
    return ", ".join(parts)


def _flows_text(info: SchemaInfo, schemas: dict) -> str:
    def named(keys) -> str:
        return ", ".join(f"{schemas[k].name} [{schemas[k].layer}]" for k in sorted(keys))
    parts = []
    reads = named(info.upstream)
    if info.outside:
        reads = ", ".join(p for p in (reads, "files or objects outside the project") if p)
    if reads:
        parts.append(f"reads {reads}")
    if info.downstream:
        parts.append(f"feeds {named(info.downstream)}")
    if info.lookups:
        parts.append(f"looks up {named(info.lookups)}")
    if info.model_reads:
        parts.append(f"read by the Power BI model ({info.model_reads} "
                     f"object{'' if info.model_reads == 1 else 's'})")
    if not parts and info.internal:
        return "data flows only between its own objects"
    return "; ".join(parts) or "no data flow"


def _schema_question(key: str, info: SchemaInfo, schemas: dict) -> Question:
    word, _strong = _word_layer(info.name)
    hint = {"name": f"its name suggests {info.layer}", "flows": "placed by its flows",
            "read by the model": "the Power BI model reads it",
            "no data flow": "no data flows through it"}.get(info.why, "")
    if info.why == "objects' names":
        hint = (f"its name says nothing, but {info.named[info.layer]} of its "
                f"{sum(info.objects.values())} objects are named like {info.layer} "
                f"({', '.join(info.examples[info.layer])}, ...)")
    if info.why.startswith("flows, though"):
        hint = (f"{info.why[len('flows, though '):]}, but nothing in the SQL loads it and it feeds the "
                f"others: data arrives here, unless a pipeline copies it from another layer")
    if info.why.endswith("disagree"):
        hint = f"its name suggests {word}, but the flows disagree"
    elif info.model_reads and word and word != STANDARD_LAYERS[-1]:
        hint = f"its name suggests {word}, but the Power BI model reads it"
    elif info.why == "flows" and info.internal and not info.flows:
        # Layers are per schema: nothing in the SQL tells the layers of a single schema apart.
        hint = ("all its flows are between its own objects: layers are given to whole schemas, so it "
                "forms a single layer; ignoring it would leave out all of them")
    if len(info.named) > 1:
        mixed = ", ".join(f"{layer}: {count} ({', '.join(info.examples[layer])})"
                          for layer, count in sorted(info.named.items(), key=lambda i: STANDARD_LAYERS.index(i[0])))
        hint = (f"{hint}; " if hint else "") + (
            f"its objects are named like different layers ({mixed}): a layer holds whole schemas, so "
            f"to tell them apart in the lineage the objects of each layer need a schema of their own")
    elif info.why == "flows" and not info.upstream and not info.outside:
        # The SQL can't tell data arriving from a source from data copied by a pipeline.
        hint = ("nothing in the SQL loads its tables: data arrives here, unless a pipeline or a "
                "generic procedure copies it from another layer")
    both_ways = sorted(info.upstream & info.downstream)
    if both_ways:
        names = ", ".join(schemas[k].name for k in both_ways)
        hint = (f"{hint}; " if hint else "") + (f"it reads and feeds {names} as much: the flows "
                                                f"alone can't order them")
    if info.lookups:
        names = ", ".join(schemas[k].name for k in sorted(info.lookups))
        hint = (f"{hint}; " if hint else "") + (f"it also reads {names}, fewer columns than it feeds "
                                                f"it: taken as lookups")
    others = [layer for layer in (*STANDARD_LAYERS, IGNORE) if layer != info.layer]
    otherwise = ", ".join(others) + " or the name of a new layer"
    details = [f"{_objects_text(info)}; {_flows_text(info, schemas)}" + (f" ({hint})" if hint else "")]
    return Question(f"schema.{key}", f"Which layer is schema '{info.name}' in?", info.layer, otherwise,
                    details)


# ---------------------------------------------------------------------------
# Loads the SQL doesn't show
# ---------------------------------------------------------------------------

def _data_objects(lineage) -> dict:
    """The tables, views and functions of the project outside the ignored schemas, by label (lower)."""
    return {o.label.lower(): o for o in lineage.analysis.objects.values()
            if o.in_project and o.kind in DATA_KINDS and not lineage.config.is_ignored(o.schema)}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _manual_loads(given, objects: dict, used, not_applied) -> dict:
    """{target label (lower): DraftRule, or None when the answer is 'none'} from the 'load.' answers."""
    result: dict[str, DraftRule | None] = {}
    for key, value in given.items():
        if not key.startswith("load."):
            continue
        target = objects.get(key[len("load."):])
        if target is None:
            continue                            # reported as not applied
        text = _text(value)
        if text.lower() in _NONE or not text:
            result[target.label.lower()] = None
            used.add(key)
            continue
        external = text.lower().startswith("external:")
        if external:
            source = text.split(":", 1)[1].strip()
            if not source:
                not_applied.append(f"{key}: give the system after 'external:', e.g. 'external:erp.orders'")
                continue
        else:
            found = objects.get(_normalize(text))
            if found is None or found is target:
                not_applied.append(f"{key}: '{text}' is not another object of the project (a system "
                                   f"outside it: 'external:{text}')")
                continue
            source = found.label
        result[target.label.lower()] = DraftRule(
            f"load-{_slug(target.label)}", re.escape(target.label),
            source.replace("{", "{{").replace("}", "}}"), external)
        used.add(key)
    return result


def _decide_rule(suggestion, given, used, not_applied, questions) -> bool:
    """Whether a suggested rule goes in the file: the answer, else the proposal (and a question)."""
    key = f"rule.{suggestion.name.lower()}"
    clean = not suggestion.already_loaded and (suggestion.external
                                               or suggestion.found == len(suggestion.matched))
    if key in given:
        decision = _yes(given[key])
        if decision is not None:
            used.add(key)
            return decision
        not_applied.append(f"{key}: answer yes or no")
    questions.append(_rule_question(suggestion, key, "yes" if clean else "no"))
    return clean


def _rule_question(suggestion, key: str, proposed: str) -> Question:
    count = len(suggestion.matched)
    if suggestion.external:
        text = "Are these tables named after the source system they come from?"
        details = [suggestion.note[len("possible: "):] if suggestion.note.startswith("possible: ")
                   else suggestion.note]
        if suggestion.matched:
            example = suggestion.matched[0]
            schema, _dot, name = example.partition(".")
            rule = Rule(name=suggestion.name, match=re.compile(suggestion.match, re.IGNORECASE),
                        source=suggestion.source, external=True)
            details.append(f"With 'yes' the lineage starts at the system: {example} comes from "
                           f"{rule.source_for(schema, name)}, outside the project.")
    else:
        text = ("Are these tables loaded outside the SQL (by a pipeline or a generic procedure) "
                "from the objects named like them?")
        shown = ", ".join(f"{t} <- {s}" for t, s in suggestion.pairs[:5])
        more = f" ... and {len(suggestion.pairs) - 5} more" if len(suggestion.pairs) > 5 else ""
        details = [shown + more]
    details.append(f"Rule: match '{suggestion.match}', from '{suggestion.source}': {count} "
                   f"object{'' if count == 1 else 's'}, {suggestion.found} with their source.")
    if suggestion.already_loaded:
        details.append(f"[!] It also matches tables the SQL already loads: "
                       f"{', '.join(suggestion.already_loaded[:5])}.")
    missing = count - suggestion.found
    if missing and not suggestion.external:
        details.append(f"[!] {missing} of them would not find their source.")
    for target, others in list(suggestion.alternatives.items())[:3]:
        details.append(f"Other possible sources of {target}: {', '.join(others)} "
                       f"(answer load.{target} to pick one).")
    return Question(key, text, proposed, "no" if proposed == "yes" else "yes", details)


def _unexplained(objects: dict, targets: list[str], rules: list[DraftRule]) -> list[str]:
    """The tables nothing loads that no rule of the draft loads either (the first match wins)."""
    compiled = [Rule(name=r.name, match=re.compile(r.match, re.IGNORECASE), source=r.source,
                     external=r.external) for r in rules]
    result = []
    for label in targets:
        obj = objects.get(label.lower())
        if obj is None:
            continue
        explained = False
        for rule in compiled:
            source = rule.source_for(obj.schema, obj.name)
            if source is not None:
                explained = rule.external or _normalize(source) in objects
                break
        if not explained:
            result.append(label)
    return result


def _load_question(obj, files=()) -> Question:
    columns = obj.columns or []
    if files:
        details = [f"It only reads {', '.join(files[:3])}{' ...' if len(files) > 3 else ''}, which nothing "
                   f"in the project writes (a pipeline copies the data there), and no rule of the draft "
                   f"loads it."]
    else:
        details = ["Nothing in the SQL writes it, and no rule of the draft loads it."]
    if columns:
        details.append(f"Columns: {', '.join(columns[:8])}{' ...' if len(columns) > 8 else ''}")
    return Question(f"load.{obj.label.lower()}", f"What loads '{obj.label}'?", "none",
                    "its source in the project ('schema.object'), or 'external:<system.table>' for a "
                    "system outside the project", details)


# ---------------------------------------------------------------------------
# Columns the ETL creates
# ---------------------------------------------------------------------------

def _looks_technical(name: str) -> bool:
    words = _words(name)
    if not words:
        return False
    if len(words) > 1 and (words[0] in _TECH_PREFIXES or words[-1] in _TECH_SUFFIXES):
        return True
    if words[-1] in _TIME_WORDS and any(w in _LOAD_WORDS for w in words[:-1]):
        return True
    if len(words) > 1 and words[0] in _RUN_WORDS and words[-1] in _RUN_ID_WORDS:
        return True
    return tuple(words[-2:]) in _TECH_PAIRS


def technical_candidates(lineage) -> list[tuple[str, list[str], int]]:
    """
    [(pattern, column names, tables)] for the columns no source fills in the
    tables the SQL loads (after the first layer): those named like ETL
    columns, or without source in several tables. Names sharing an ETL prefix
    or suffix ('etl_', '_sk') become one pattern, unless it would also cover
    a column that a source fills and that is never without one: business
    data, as when every column of a warehouse starts with 'dw_'.
    """
    config = lineage.config
    first = config.layer_names[0] if config.layer_names else ""
    loaded = lineage.objects.incoming()
    filled = lineage.columns.incoming()
    sourceless: dict[str, tuple[str, set]] = {}
    sourced: set[str] = set()
    for obj in _data_objects(lineage).values():
        if config.layer_of(obj.schema) in ("", first):
            continue
        for column in obj.columns or []:
            if filled.get(f"{obj.key}|{column.lower()}"):
                sourced.add(column.lower())
            elif loaded.get(obj.key):
                sourceless.setdefault(column.lower(), (column, set()))[1].add(obj.label)
    names = {lower: entry for lower, entry in sourceless.items()
             if _looks_technical(entry[0]) or len(entry[1]) >= _TECH_MIN_TABLES}

    groups: dict[str, list[str]] = {}
    for lower in names:
        parts = lower.split("_")
        if len(parts) > 1 and parts[0] in _TECH_PREFIXES:
            groups.setdefault(re.escape(parts[0]) + r"_\w+", []).append(lower)
        elif len(parts) > 1 and parts[-1] in _TECH_SUFFIXES:
            groups.setdefault(r"\w+_" + re.escape(parts[-1]), []).append(lower)
    result, covered = [], set()
    for pattern, members in sorted(groups.items()):
        compiled = re.compile(pattern, re.IGNORECASE)
        if len(members) < 2 or any(compiled.fullmatch(c) and c not in names for c in sourced):
            continue
        tables = set().union(*(names[m][1] for m in members))
        result.append((pattern, sorted(names[m][0] for m in members), len(tables)))
        covered.update(members)
    for lower, (column, tables) in sorted(names.items()):
        if lower not in covered:
            result.append((re.escape(column), [column], len(tables)))
    return result


def _technical(lineage, given, used, not_applied, questions) -> list[str]:
    candidates = technical_candidates(lineage)
    proposed = [pattern for pattern, _names, _tables in candidates]
    if "technical_columns" in given:
        value = given["technical_columns"]
        decision = None if isinstance(value, (list, tuple)) else _yes(value)
        if decision is not None:
            used.add("technical_columns")
            return proposed if decision else []
        listed = [str(v).strip() for v in value] if isinstance(value, (list, tuple)) else \
            [p.strip() for p in re.split(r",\s*(?![^{]*\})", _text(value))]
        listed = [p for p in listed if p]
        invalid = []
        for pattern in listed:
            try:
                re.compile(pattern)
            except re.error:
                invalid.append(pattern)
        if listed and not invalid:
            used.add("technical_columns")
            return listed
        not_applied.append("technical_columns: answer yes, no or the regular expressions to keep"
                           + (f" ({', '.join(invalid)} not valid)" if invalid else ""))
    if not candidates:
        return []
    details = [f"'{pattern}': {', '.join(names[:6])}{' ...' if len(names) > 6 else ''} "
               f"({tables} table{'' if tables == 1 else 's'})" for pattern, names, tables in candidates]
    questions.append(Question(
        "technical_columns",
        "Are these columns created by the ETL itself (load dates, run ids, surrogate keys, hashes)? "
        "No source column fills them.",
        "yes", "no, or the list of the patterns to keep", details))
    return proposed


# ---------------------------------------------------------------------------
# The file and the response
# ---------------------------------------------------------------------------

def _scalar(text: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", text) and text.lower() not in _YAML_WORDS:
        return text
    return _quoted(text)


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def render_yaml(draft: Draft) -> str:
    """The draft as 'lineage.yaml', commented like examples/lineage.example.yaml."""
    lines = [f"# lineage.yaml of the database project '{draft.project.name}'.",
             f"# {DRAFT_MARKER} from the SQL of the project and the answers given.",
             "# Check it with test_lineage_config; the guide: get_lineage_config_guide.",
             "", "version: 1", "", *DRAFT_LINES, ""]
    if draft.layers:
        lines += ["# The layers in flow order (the first is the closest to the sources).", "layers:"]
        for layer, members in draft.layers:
            value = _scalar(members[0]) if len(members) == 1 else \
                "[" + ", ".join(_scalar(m) for m in members) + "]"
            lines.append(f"  {_scalar(layer)}: {value}")
        lines.append("")
    if draft.ignore:
        lines += ["# Schemas without data flows: left out of the lineage.",
                  "ignore: [" + ", ".join(_scalar(s) for s in draft.ignore) + "]", ""]
    if draft.outside:
        shown = ", ".join(draft.outside[:MAX_OUTSIDE_SHOWN])
        more = len(draft.outside) - MAX_OUTSIDE_SHOWN
        lines += ["# Read by the SQL but not defined in the project (a synonym, another database): "
                  "shown as sources.", f"#   {shown}" + (f" ... and {more} more" if more > 0 else ""), ""]
    lines += ["# The schema of names written without one.",
              f"default_schema: {_scalar(draft.default_schema)}", ""]
    if draft.rules:
        lines += ["# Loads the SQL doesn't show. The first matching rule wins.", "rules:"]
        for rule in draft.rules:
            lines += [f"  - name: {_scalar(rule.name)}", f"    match: {_quoted(rule.match)}",
                      f"    from: {_quoted(rule.source)}"]
            if rule.external:
                lines.append("    external: true")
        lines.append("")
    if draft.technical:
        lines += ["# Columns created by the ETL itself: surrogate keys, hashes, load dates.",
                  "technical_columns: [" + ", ".join(_quoted(p) for p in draft.technical) + "]", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def format_draft(draft: Draft, include_schemas: bool = True, include_details: bool = True) -> str:
    project = draft.project
    lines = [f"=== LINEAGE CONFIGURATION DRAFT: {project.name} ===", f"Database project: {project.sqlproj}"]
    if draft.existing:
        lines.append(f"Existing configuration: {draft.existing}. This draft starts from the SQL, not "
                     f"from that file; saving it replaces the file only with 'overwrite'.")
    lines.append(f"Names without schema: {draft.default_schema} ({draft.default_from}).")
    if draft.outside:
        lines.append(f"Read by the SQL but not defined in the project, shown as sources: "
                     f"{', '.join(draft.outside[:MAX_OUTSIDE_SHOWN])}"
                     + (f" ... and {len(draft.outside) - MAX_OUTSIDE_SHOWN} more"
                        if len(draft.outside) > MAX_OUTSIDE_SHOWN else "") + ".")
    if draft.model:
        lines.append(f"Power BI model: {draft.model} (the schemas it reads are proposed as the last layer).")
    if draft.applied or draft.not_applied:
        lines.append(f"Answers applied: {draft.applied}.")
    for entry in draft.not_applied:
        lines.append(f"  [!] Not applied: {entry}")

    if include_schemas:
        by_key = {info.name.lower(): info for info in draft.schemas}
        width = max(len(i.name) for i in draft.schemas)
        layer_width = max(len(i.layer) for i in draft.schemas)
        lines += ["", "Schemas, in flow order (layer, why, objects and flows):"]
        for info in draft.schemas:
            why = info.why + ("" if info.sure else "; to confirm")
            lines.append(f"  {info.name:<{width}}  {info.layer:<{layer_width}}  ({why})  "
                         f"{_objects_text(info)}; {_flows_text(info, by_key)}")

    loads = [q for q in draft.questions if q.key.startswith("load.")]
    shown = [q for q in draft.questions if not q.key.startswith("load.")] + loads[:MAX_LOAD_QUESTIONS]
    lines.append("")
    if shown:
        lines.append(f"Open questions: {len(draft.questions)}. Ask the user (all together), then call "
                     f"draft_lineage_config again with every answer given so far in 'answers', e.g. "
                     f"{{\"{shown[0].key}\": \"{shown[0].proposed}\"}}. Unanswered questions keep the "
                     f"proposed answer.")
        for number, question in enumerate(shown, 1):
            lines.append(f"  {number}. {question.key}: {question.text}")
            if include_details:
                lines += [f"     {detail}" for detail in question.details]
            lines.append(f"     Proposed: {question.proposed}. Otherwise: {question.otherwise}.")
        if len(loads) > MAX_LOAD_QUESTIONS:
            rest = [q.key[len("load."):] for q in loads[MAX_LOAD_QUESTIONS:]]
            lines.append(f"  ... and {len(rest)} more tables nothing loads, asked after these: "
                         f"{', '.join(rest[:15])}{' ...' if len(rest) > 15 else ''}")
        lines.append("Any other choice can be changed with the same keys: schema.<schema> (a layer or "
                     "'ignore'), layer_order, default_schema, rule.<name> (yes/no), load.<schema.table> "
                     "(its source, 'external:<system.table>' or 'none'), technical_columns.")
    else:
        lines.append("No open question: the file below is complete.")

    lines += ["", "Draft of lineage.yaml" + (" (open questions at their proposed answer):" if shown else ":"),
              "", "```yaml", *draft.text.rstrip("\n").split("\n"), "```", ""]
    explained = len(draft.targets) - len(draft.unexplained)
    if draft.targets:
        lines.append(f"Tables nothing loads in the SQL, or that only read files a pipeline writes: "
                     f"{len(draft.targets)}; {explained} loaded by the rules of this draft" + (f", {len(draft.unexplained)} still unexplained."
                                               if draft.unexplained else "."))
    lines.append("Next: " + ("answer the open questions, then " if shown else "")
                 + "save it with save_lineage_config (full access mode) or as 'lineage.yaml' next to "
                   "the .sqlproj, then run test_lineage_config.")
    return "\n".join(lines)
