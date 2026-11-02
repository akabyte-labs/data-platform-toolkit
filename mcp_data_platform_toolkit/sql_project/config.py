"""
The lineage configuration of a database project, read from 'lineage.yaml'.

It says how the project realizes the medallion architecture: which schemas
form each layer, in flow order, and the patterns of the loads that the SQL
doesn't show (a pipeline or a generic procedure fills a table from another
object). Everything the SQL does show is found by the parser and needs no
configuration.

Two places hold it: the repository's 'lineage.yaml', next to the .sqlproj,
shared with the team when someone commits it; and the user's own,
'projects/<repository>/<project>.lineage.yaml' in the user's folder (see
`user_folder`), outside the repository. The repository's wins. Without
either, the standard medallion applies: schemas named bronze, silver and
gold. The writing instructions are in 'lineage_config_guide.md', in the
package.
"""

from __future__ import annotations

import os
import re
import string
from dataclasses import dataclass, field

import yaml
from yaml.constructor import ConstructorError, SafeConstructor

from mcp_data_platform_toolkit.mcp_common import user_folder

CONFIG_FILE_NAMES = ("lineage.yaml", "lineage.yml")
# The user's own configuration: '<project>.lineage.yaml', named after the
# .sqlproj, since a repository may hold more than one database project.
PERSONAL_SUFFIX = ".lineage.yaml"
DEFAULT_LAYERS = (("bronze", ("bronze",)), ("silver", ("silver",)), ("gold", ("gold",)))
DEFAULT_SCHEMA = "dbo"

# The layer of the nodes outside the project: source systems and files.
SOURCE_LAYER = "source"

_TOP_KEYS = ("version", "draft", "layers", "ignore", "default_schema", "rules", "technical_columns")
_RULE_KEYS = ("name", "match", "from", "columns", "external")
_TEMPLATE_BUILTINS = ("schema", "name")


class ConfigError(ValueError):
    """The configuration file has problems: all of them, each with its line."""

    def __init__(self, path: str, problems: list[str]) -> None:
        self.path = path
        self.problems = list(problems)
        listed = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(
            f"'{path}' has {len(self.problems)} problem(s):\n{listed}\n"
            f"See 'get_lineage_config_guide' for how to write it."
        )


@dataclass(frozen=True)
class Rule:
    """A load the SQL doesn't show: objects matching `match` are loaded from `source`."""
    name: str
    match: re.Pattern
    source: str                                     # template, e.g. 'core.{entity}'
    columns: dict = field(default_factory=dict)     # target column (lower) -> source column
    external: bool = False                          # the source is outside the project
    line: int = 0

    def source_for(self, schema: str, name: str) -> str | None:
        """The source of `schema.name`, or None if the rule doesn't match it."""
        found = self.match.fullmatch(f"{schema}.{name}")
        if found is None:
            return None
        values = {"schema": schema, "name": name}
        values.update({k: v for k, v in found.groupdict().items() if v is not None})
        return self.source.format(**values)


@dataclass(frozen=True)
class LineageConfig:
    path: str                                       # "" = no file, standard medallion
    layers: tuple = DEFAULT_LAYERS                  # ((layer, (schema, ...)), ...) in flow order
    ignore: tuple = ()
    default_schema: str = DEFAULT_SCHEMA
    rules: tuple = ()
    technical_columns: tuple = ()
    draft: bool = False                             # generated, not checked yet by the user
    provisional: bool = False                       # proposed from the SQL, not saved anywhere

    @property
    def blank(self) -> bool:
        """True when it says nothing: the standard layers and nothing else."""
        return (self.layers == DEFAULT_LAYERS and not self.ignore and not self.rules
                and not self.technical_columns)

    @property
    def layer_names(self) -> list[str]:
        return [name for name, _schemas in self.layers]

    def layer_of(self, schema: str) -> str:
        target = (schema or "").lower()
        for name, schemas in self.layers:
            if target in (s.lower() for s in schemas):
                return name
        return ""

    def layer_index(self, layer: str) -> int | None:
        names = self.layer_names
        return names.index(layer) if layer in names else None

    def is_ignored(self, schema: str) -> bool:
        return (schema or "").lower() in (s.lower() for s in self.ignore)

    def is_technical(self, column: str) -> bool:
        return any(p.fullmatch(column or "") for p in self.technical_columns)


def find_config_file(project_folder: str) -> str:
    """The repository's 'lineage.yaml' (or '.yml'), in the root of the database project, or ''."""
    for name in CONFIG_FILE_NAMES:
        path = os.path.join(project_folder, name)
        if os.path.isfile(path):
            return path
    return ""


def personal_config_path(project_folder: str, sqlproj: str) -> str:
    """Where the user's own configuration of the project goes. Creates nothing."""
    project = user_folder.project_identity(project_folder)
    name = user_folder.safe_file_name(os.path.splitext(os.path.basename(sqlproj))[0])
    return str(user_folder.project_dir(project) / f"{name}{PERSONAL_SUFFIX}")


def personal_config_file(project_folder: str, sqlproj: str) -> str:
    """
    The user's own configuration of the project, outside its repository (it
    may not exist). The project's folder is created, so the user finds where
    the file goes.
    """
    project = user_folder.project_identity(project_folder)
    path = personal_config_path(project_folder, sqlproj)
    user_folder.ensure_project_dir(project)
    return path


def find_project_config(project_folder: str, sqlproj: str) -> str:
    """The configuration that applies: the repository's, else the user's own, else ''."""
    shared = find_config_file(project_folder)
    if shared:
        return shared
    personal = personal_config_file(project_folder, sqlproj)
    return personal if os.path.isfile(personal) else ""


def load_config(path: str) -> LineageConfig:
    """The configuration in `path`, or the standard medallion when `path` is ''."""
    if not path:
        return LineageConfig(path="")
    with open(path, encoding="utf-8-sig") as handle:
        text = handle.read()
    return parse_config(text, path)


def parse_config(text: str, path: str = "lineage.yaml") -> LineageConfig:
    """Validates the whole file and reports every problem, not only the first one."""
    data, lines, problems = _compose(text)
    if problems:
        raise ConfigError(path, problems)
    if data is None:
        return LineageConfig(path=path)             # empty file: everything at its default
    if not isinstance(data, dict):
        raise ConfigError(path, ["line 1: the file must be a mapping of keys (layers, rules, ...)"])

    def where(key_path: str) -> str:
        return f"line {lines.get(key_path, 0)}, {key_path}"

    for key in data:
        if key not in _TOP_KEYS:
            problems.append(f"{where(key)}: unknown key (allowed: {', '.join(_TOP_KEYS)})")

    version = data.get("version", 1)
    if version != 1:
        problems.append(f"{where('version')}: only version 1 exists")
    draft = data.get("draft", False)
    if not isinstance(draft, bool):
        problems.append(f"{where('draft')}: must be true (generated, not checked yet) or false")
        draft = True

    layers = _layers(data.get("layers"), where, problems)
    ignore = _schema_list(data.get("ignore"), "ignore", where, problems)
    taken = {s.lower(): layer for layer, schemas in layers for s in schemas}
    for schema in ignore:
        if schema.lower() in taken:
            problems.append(f"{where('ignore')}: schema '{schema}' is also in layer "
                            f"'{taken[schema.lower()]}'")

    default_schema = data.get("default_schema", DEFAULT_SCHEMA)
    if not isinstance(default_schema, str) or not default_schema.strip():
        problems.append(f"{where('default_schema')}: must be a schema name")
        default_schema = DEFAULT_SCHEMA

    technical = []
    for i, value in enumerate(_as_list(data.get("technical_columns"), "technical_columns",
                                       where, problems)):
        compiled = _regex(value, f"technical_columns[{i}]", where, problems)
        if compiled is not None:
            technical.append(compiled)

    rules = []
    names: set[str] = set()
    for i, raw in enumerate(_as_list(data.get("rules"), "rules", where, problems)):
        rule = _rule(raw, f"rules[{i}]", lines, where, problems)
        if rule is None:
            continue
        if rule.name.lower() in names:
            problems.append(f"{where(f'rules[{i}].name')}: another rule is called '{rule.name}'")
        names.add(rule.name.lower())
        rules.append(rule)

    if problems:
        raise ConfigError(path, problems)
    return LineageConfig(path=path, layers=tuple(layers), ignore=tuple(ignore),
                         default_schema=default_schema.strip(), rules=tuple(rules),
                         technical_columns=tuple(technical), draft=draft)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _layers(value, where, problems) -> list[tuple[str, tuple]]:
    if value is None:
        return list(DEFAULT_LAYERS)
    if not isinstance(value, dict) or not value:
        problems.append(f"{where('layers')}: must map each layer to its schemas, "
                        f"in flow order (e.g. 'bronze: [raw]')")
        return list(DEFAULT_LAYERS)
    layers, seen = [], {}
    for name, schemas in value.items():
        key_path = f"layers.{name}"
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{where('layers')}: layer names must be text")
            continue
        if name.strip().lower() == SOURCE_LAYER:
            problems.append(f"{where(key_path)}: '{SOURCE_LAYER}' is reserved for the "
                            f"systems and files outside the project")
        listed = _schema_list(schemas, key_path, where, problems)
        if not listed:
            problems.append(f"{where(key_path)}: list at least one schema")
        for schema in listed:
            other = seen.get(schema.lower())
            if other is not None:
                problems.append(f"{where(key_path)}: schema '{schema}' is already in layer '{other}'")
            seen[schema.lower()] = name
        layers.append((name.strip(), tuple(listed)))
    return layers


def _schema_list(value, key_path, where, problems) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(s, str) and s.strip() for s in items):
        problems.append(f"{where(key_path)}: must be a schema name or a list of schema names")
        return []
    return [_unbracket(s) for s in items]


def _rule(raw, key_path, lines, where, problems) -> Rule | None:
    if not isinstance(raw, dict):
        problems.append(f"{where(key_path)}: a rule is a mapping with name, match and from")
        return None
    for key in raw:
        if key not in _RULE_KEYS:
            problems.append(f"{where(f'{key_path}.{key}')}: unknown key "
                            f"(allowed: {', '.join(_RULE_KEYS)})")
    missing = [k for k in ("name", "match", "from") if not raw.get(k)]
    if missing:
        problems.append(f"{where(key_path)}: missing {', '.join(missing)}")
        return None
    name = raw["name"]
    if not isinstance(name, str):
        problems.append(f"{where(f'{key_path}.name')}: must be text")
        return None
    match = _regex(raw["match"], f"{key_path}.match", where, problems)
    template = raw["from"]
    external = raw.get("external", False)
    if not isinstance(external, bool):
        problems.append(f"{where(f'{key_path}.external')}: must be true or false")
        external = False
    if not isinstance(template, str):
        problems.append(f"{where(f'{key_path}.from')}: must be text, e.g. 'core.{{entity}}'")
        return None
    if match is not None and not _template_ok(template, match, f"{key_path}.from", where, problems):
        match = None
    if not external and "." not in template:
        problems.append(f"{where(f'{key_path}.from')}: must give 'schema.object' "
                        f"(or set 'external: true' for a source system)")
    columns = raw.get("columns") or {}
    if not isinstance(columns, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in columns.items()):
        problems.append(f"{where(f'{key_path}.columns')}: must map target column to source "
                        f"column, e.g. 'CustomerKey: CustomerId'")
        columns = {}
    if match is None:
        return None
    return Rule(name=name.strip(), match=match, source=template,
                columns={k.lower(): v for k, v in columns.items()}, external=external,
                line=lines.get(key_path, 0))


def _template_ok(template, match, key_path, where, problems) -> bool:
    allowed = set(match.groupindex) | set(_TEMPLATE_BUILTINS)
    try:
        fields = [f for _text, f, _spec, _conv in string.Formatter().parse(template) if f is not None]
    except ValueError as exc:
        problems.append(f"{where(key_path)}: invalid template ({exc})")
        return False
    unknown = [f for f in fields if f not in allowed]
    if unknown:
        problems.append(
            f"{where(key_path)}: {', '.join('{' + f + '}' for f in unknown)} is not a named "
            f"group of 'match' (groups: {', '.join(sorted(match.groupindex)) or 'none'}; "
            f"always available: {{schema}}, {{name}})")
        return False
    return True


def _regex(value, key_path, where, problems) -> re.Pattern | None:
    if not isinstance(value, str) or not value:
        problems.append(f"{where(key_path)}: must be a regular expression in single quotes")
        return None
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as exc:
        problems.append(f"{where(key_path)}: invalid regular expression ({exc})")
        return None


def _as_list(value, key_path, where, problems) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"{where(key_path)}: must be a list (one '- ' item per line)")
        return []
    return value


def _unbracket(name: str) -> str:
    text = name.strip()
    if len(text) >= 2 and text[0] == "[" and text[-1] == "]":
        return text[1:-1]
    return text


# ---------------------------------------------------------------------------
# YAML with the line of every key
# ---------------------------------------------------------------------------

def _compose(text: str) -> tuple[object, dict[str, int], list[str]]:
    """
    The document as Python values, the line of every key path
    ('rules[1].match' -> 12) and the problems found while reading it.

    `yaml.safe_load` gives no lines and keeps the last of two duplicate keys
    without a word: reading the node tree gives both.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else 0
        reason = getattr(exc, "problem", None) or str(exc)
        return None, {}, [f"line {line}: not valid YAML ({reason})"]
    if root is None:
        return None, {}, []

    constructor = SafeConstructor()
    lines: dict[str, int] = {}
    problems: list[str] = []

    def build(node, key_path: str):
        lines.setdefault(key_path, node.start_mark.line + 1)
        if isinstance(node, yaml.MappingNode):
            result = {}
            for key_node, value_node in node.value:
                key = build(key_node, key_path)
                if not isinstance(key, (str, int, float, bool)):
                    problems.append(f"line {key_node.start_mark.line + 1}: keys must be text")
                    continue
                child = f"{key_path}.{key}" if key_path else str(key)
                if key in result:
                    problems.append(f"line {key_node.start_mark.line + 1}, {child}: "
                                    f"written twice, only one is allowed")
                lines[child] = key_node.start_mark.line + 1
                result[key] = build(value_node, child)
            return result
        if isinstance(node, yaml.SequenceNode):
            return [build(item, f"{key_path}[{i}]") for i, item in enumerate(node.value)]
        try:
            return constructor.construct_object(node)
        except ConstructorError as exc:
            problems.append(f"line {node.start_mark.line + 1}: {exc.problem}")
            return None

    return build(root, ""), lines, problems
