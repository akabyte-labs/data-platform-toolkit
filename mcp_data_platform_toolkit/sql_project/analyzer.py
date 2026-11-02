"""
The lineage of a database project, object by object and column by column.

Two passes. The first reads every file: the objects it defines (tables,
views, external tables, inline functions) and the statements that write
data (view and function bodies, INSERT, MERGE, UPDATE, SELECT INTO,
CREATE TABLE AS). The second resolves the names against the whole project
and follows every column through the statements, with the columns of each
object known: `SELECT *` and unqualified columns are only resolvable that
way.

Names are compared case-insensitively, as SQL Server does with the usual
collations, and reported as declared. A name that can't be attributed to a
single object is reported, never guessed.

Temporary tables and table variables are steps inside one procedure: their
edges are composed (source -> #t -> target becomes source -> target) and
they don't appear in the result.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import to_node
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope
from sqlglot.schema import MappingSchema

from mcp_data_platform_toolkit.sql_project import tsql_scanner as scanner
from mcp_data_platform_toolkit.sql_project.config import LineageConfig
from mcp_data_platform_toolkit.sql_project.sql_files import MAX_SQL_FILE_BYTES, SqlFile, read_sql_text

# sqlglot logs a warning for every construct it reads as opaque text: on a
# real project, hundreds of lines on stderr for what the summary reports once.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

DIALECT = "tsql"

# Kinds of objects that hold data (procedures don't: they are where
# statements live).
LOCAL_KINDS = ("temp", "table_variable")


@dataclass
class SqlObject:
    key: str                        # lowercase identity: 'schema.name', 'file:path', '#t@owner'
    schema: str
    name: str
    kind: str                       # table, view, external_table, function, file, source,
                                    # temp, table_variable, unknown
    file: str = ""
    line: int = 0
    columns: list[str] | None = None
    in_project: bool = True
    location: str = ""
    flag: str = ""                  # 'not in project', 'ambiguous'
    definition: "Statement | None" = None
    computed: dict = field(default_factory=dict)        # column -> [columns of the same table]

    @property
    def label(self) -> str:
        if self.kind == "file":
            return f"file:{self.location}"
        if self.kind in LOCAL_KINDS:
            return self.name
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass
class Statement:
    kind: str                       # view, function, create_as, insert, merge, update, select_into
    expression: exp.Expression
    file: str
    line: int
    owner: str = ""                 # procedure (or file) the statement belongs to
    reads: dict = field(default_factory=dict)           # OPENROWSET placeholder -> FileRead
    target_key: str = ""
    column_names: list[str] | None = None                # CREATE VIEW v (A, B)

    @property
    def evidence(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(eq=False)
class Edge:
    source: str
    target: str
    kind: str
    evidence: str
    rule: str = ""


@dataclass
class Analysis:
    objects: dict
    object_edges: list
    column_edges: list
    files: int = 0
    statements: int = 0
    problems: dict = field(default_factory=dict)        # category -> [text]
    rules: dict = field(default_factory=dict)           # rule name -> {matched, found, missing}

    def problem(self, category: str, text: str) -> None:
        self.problems.setdefault(category, []).append(text)


def column_key(object_key: str, column: str) -> str:
    return f"{object_key}|{column.lower()}"


# ---------------------------------------------------------------------------
# Pass 1: objects and statements
# ---------------------------------------------------------------------------

def _identifier(node) -> str:
    if node is None:
        return ""
    if isinstance(node, exp.Identifier):
        return node.this
    return getattr(node, "name", "") or ""


def _table_parts(table: exp.Table) -> tuple[str, str, str]:
    """
    (prefix, schema, name) as written. The prefix is the database, or server
    and database, of a three- or four-part name; the name of a table function
    (`FROM dbo.f(1)`) is its function name.
    """
    parts = [p.name for p in table.parts]
    if not parts:
        return "", "", ""
    return ".".join(parts[:-2]), parts[-2] if len(parts) >= 2 else "", parts[-1]


def normalize_applies(expression: exp.Expression) -> None:
    """
    CROSS / OUTER APPLY as a plain join, in place.

    sqlglot follows an APPLY subquery as a correlated subquery: the column it
    returns was attributed to the outer column of its WHERE (`b.id = a.id`)
    instead of its own source. As a join its projection maps as any derived
    table. A table function applied (`OUTER APPLY dbo.f(a.id)`) becomes a
    table reference, so it is a dependency like a view.
    """
    for join in list(expression.find_all(exp.Join)):
        lateral = join.this
        if not isinstance(lateral, exp.Lateral):
            continue
        alias = lateral.args.get("alias")
        inner = lateral.this
        if isinstance(inner, exp.Subquery):
            replacement = inner.copy()
            if alias is not None and not replacement.args.get("alias"):
                replacement.set("alias", alias.copy())
        else:
            function = inner.expression if isinstance(inner, exp.Dot) else inner
            if not isinstance(function, exp.Anonymous):
                continue
            schema = inner.this if isinstance(inner, exp.Dot) else None
            replacement = exp.Table(this=exp.to_identifier(function.name),
                                    db=exp.to_identifier(_identifier(schema)) if schema else None,
                                    alias=alias.copy() if alias is not None else None)
        join.set("this", replacement)
        join.set("kind", "CROSS")
        join.set("side", None)


def _local_name(table: exp.Table) -> str | None:
    """'#t' or '@t' for a temporary table or a table variable, else None."""
    this = table.this
    if isinstance(this, exp.Parameter):
        return "@" + this.name
    if isinstance(this, exp.Identifier) and (this.args.get("temporary") or this.args.get("global_")):
        return ("##" if this.args.get("global_") else "#") + this.this
    return None


def _first_query(node) -> exp.Expression | None:
    """The outermost SELECT / UNION under `node`."""
    if node is None:
        return None
    if isinstance(node, (exp.Select, exp.SetOperation)):
        return node
    for child in node.iter_expressions():
        found = _first_query(child)
        if found is not None:
            return found
    return None


class _Scanner:
    """First pass over the files."""

    def __init__(self, config: LineageConfig, analysis: Analysis) -> None:
        self.config = config
        self.analysis = analysis
        self.objects = analysis.objects
        self.statements: list[Statement] = []

    def scan(self, files: list[SqlFile]) -> list[Statement]:
        for sql_file in files:
            if sql_file.size > MAX_SQL_FILE_BYTES:
                self.analysis.problem(
                    "skipped_files",
                    f"{sql_file.rel_path}: {sql_file.size // 1_000_000} MB, above the "
                    f"{MAX_SQL_FILE_BYTES // 1_000_000} MB limit")
                continue
            try:
                text = read_sql_text(sql_file.path)
            except OSError as exc:
                self.analysis.problem("parse_errors", f"{sql_file.rel_path}: not readable ({exc})")
                continue
            self.analysis.files += 1
            counter = [0]
            reads: dict = {}
            for batch in scanner.split_batches(text):
                sql, found = scanner.rewrite_openrowset(batch.text, counter)
                reads.update({r.name.lower(): r for r in found})
                try:
                    self._batch(scanner.Batch(sql, batch.line), sql_file.rel_path, reads)
                except Exception as exc:  # noqa: BLE001 - one batch must not stop the project
                    self.analysis.problem(
                        "parse_errors",
                        f"{sql_file.rel_path}:{scanner.first_line(batch)}: {type(exc).__name__}: {_short(exc)}")
        return self.statements

    # -- batches --------------------------------------------------------------

    def _batch(self, batch: scanner.Batch, rel_path: str, reads: dict) -> None:
        header = scanner.module_header(batch.text)
        if header is not None:
            self._module(header, batch, rel_path, reads)
            return
        line = scanner.first_line(batch)
        try:
            expressions = _parse(batch.text)
        except SqlglotError as exc:
            if self._table_without_parser(batch, rel_path, line, exc):
                return
            if not self._salvage(batch, rel_path, reads, owner=rel_path):
                self.analysis.problem("parse_errors", f"{rel_path}:{line}: {_short(exc)}")
            return
        opaque = False
        for expression in expressions:
            if isinstance(expression, exp.Command):
                opaque = True
                continue
            self._expression(expression, rel_path, line, reads, owner=rel_path)
        if opaque:
            self._salvage(batch, rel_path, reads, owner=rel_path)

    def _module(self, header, batch, rel_path, reads) -> None:
        kind, dotted = header
        schema, name = _split_dotted(dotted, self.config.default_schema)
        label = f"{schema}.{name}"
        line = scanner.first_line(batch)
        if kind == "trigger":
            self.analysis.problem("not_analyzed", f"{rel_path}:{line}: trigger {label}")
            return
        if kind == "function":
            function = scanner.function_kind(batch.text)
            if function == "multi_statement":
                self.analysis.problem(
                    "not_analyzed", f"{rel_path}:{line}: multi-statement function {label}")
            if function != "inline":
                return
            try:
                parsed = sqlglot.parse_one(batch.text, read=DIALECT)
            except SqlglotError as exc:
                self.analysis.problem("parse_errors", f"{rel_path}:{line}: {_short(exc)}")
                return
            normalize_applies(parsed)
            query = _first_query(parsed.args.get("expression"))
            if query is None:
                return
            obj = self._define(schema, name, "function", rel_path, line)
            if obj is not None:
                obj.definition = Statement("function", query, rel_path, line,
                                           owner=label, reads=reads, target_key=obj.key)
                self.statements.append(obj.definition)
            return
        owner = label.lower()
        body = scanner.scan_module_body(batch.text, skip_header=True)
        self._segments(body, batch, rel_path, reads, owner, label)

    def _table_without_parser(self, batch, rel_path, line, exc) -> bool:
        """
        A CREATE TABLE the parser can't read still defines its table, from its
        column list: lost, the table would look like an object outside the
        project to everything that reads it. Said, since computed columns
        don't get their sources.
        """
        definition = scanner.table_definition(batch.text)
        if definition is None:
            return False
        dotted, columns = definition
        schema, name = _split_dotted(dotted, self.config.default_schema)
        if name.startswith("#"):
            return False
        obj = self._define(schema, name, "table", rel_path, line)
        if obj is not None:
            obj.columns = columns or None
            self.analysis.problem("read_without_parser",
                                  f"{rel_path}:{line}: {obj.label} ({len(columns)} columns; {_short(exc)})")
        return True

    def _salvage(self, batch, rel_path, reads, owner) -> bool:
        """The data statements of a batch the parser couldn't read; False if there are none."""
        body = scanner.scan_module_body(batch.text, skip_header=False)
        self._segments(body, batch, rel_path, reads, owner, rel_path)
        return bool(body.segments or body.dynamic_sql or body.insert_exec)

    def _segments(self, body, batch, rel_path, reads, owner, label) -> None:
        for offset in body.dynamic_sql:
            self.analysis.problem(
                "dynamic_sql", f"{rel_path}:{scanner.line_of(batch, offset)}: {label}")
        for offset in body.insert_exec:
            self.analysis.problem(
                "not_analyzed",
                f"{rel_path}:{scanner.line_of(batch, offset)}: INSERT ... EXEC in {label}")
        for segment in body.segments:
            if segment.kind == "delete":
                continue
            line = scanner.line_of(batch, segment.offset)
            try:
                parsed = _parse(segment.text)
            except SqlglotError as exc:
                self.analysis.problem("parse_errors", f"{rel_path}:{line}: {_short(exc)}")
                continue
            for expression in parsed:
                if not isinstance(expression, exp.Command):
                    self._expression(expression, rel_path, line, reads, owner=owner)

    # -- statements -------------------------------------------------------------

    def _expression(self, expression, rel_path, line, reads, owner) -> None:
        normalize_applies(expression)
        if isinstance(expression, exp.Create):
            self._create(expression, rel_path, line, reads, owner)
            return
        kind = None
        if isinstance(expression, exp.Insert):
            kind = "insert"
        elif isinstance(expression, exp.Merge):
            kind = "merge"
        elif isinstance(expression, exp.Update):
            kind = "update"
        elif isinstance(expression, exp.Select) and expression.args.get("into") is not None:
            kind = "select_into"
            self._define_select_into(expression, rel_path, line, reads, owner)
        if kind is not None:
            self.statements.append(Statement(kind, expression, rel_path, line, owner=owner,
                                             reads=reads))

    def _create(self, expression: exp.Create, rel_path, line, reads, owner) -> None:
        kind = str(expression.args.get("kind") or "").upper()
        target = expression.this
        column_names = None
        if isinstance(target, exp.Schema):
            column_names = target.expressions
            target = target.this
        if not isinstance(target, exp.Table):
            return
        query = _first_query(expression.args.get("expression"))

        if kind == "TABLE":
            local = _local_name(target)
            properties = expression.args.get("properties")
            external = properties is not None and properties.find(exp.ExternalProperty) is not None
            location = properties.find(exp.LocationProperty) if properties is not None else None
            if local is not None:
                obj = self._define_local(local, owner, "temp", rel_path, line)
            else:
                _prefix, schema, name = _table_parts(target)
                obj = self._define(schema or self.config.default_schema, name,
                                   "external_table" if external else "table", rel_path, line)
            if obj is None:
                return
            definitions = [c for c in (column_names or []) if isinstance(c, exp.ColumnDef)]
            if definitions:
                obj.columns = [c.name for c in definitions]
                for definition in definitions:
                    for constraint in definition.args.get("constraints") or []:
                        if isinstance(constraint.kind, exp.ComputedColumnConstraint):
                            obj.computed[definition.name] = sorted(
                                {c.name for c in constraint.kind.find_all(exp.Column)})
            if location is not None:
                obj.location = location.this.name if isinstance(location.this, exp.Literal) \
                    else location.this.sql(DIALECT)
            if query is not None:
                obj.definition = Statement("create_as", query, rel_path, line, owner=owner,
                                           reads=reads, target_key=obj.key)
                self.statements.append(obj.definition)
            return

        if kind == "VIEW" and query is not None:
            _prefix, schema, name = _table_parts(target)
            obj = self._define(schema or self.config.default_schema, name, "view", rel_path, line)
            if obj is None:
                return
            names = [_identifier(c) for c in column_names or [] if isinstance(c, exp.Identifier)]
            obj.definition = Statement("view", query, rel_path, line, owner=obj.key, reads=reads,
                                       target_key=obj.key, column_names=names or None)
            self.statements.append(obj.definition)

    def _define_select_into(self, expression: exp.Select, rel_path, line, reads, owner) -> None:
        """SELECT ... INTO creates its target: the first one found defines its columns."""
        target = expression.args["into"].this
        if not isinstance(target, exp.Table):
            return
        local = _local_name(target)
        if local is not None:
            if _local_key(local, owner) in self.objects:
                return
            obj = self._define_local(local, owner, "temp", rel_path, line)
        else:
            _prefix, schema, name = _table_parts(target)
            if f"{schema or self.config.default_schema}.{name}".lower() in self.objects:
                return
            obj = self._define(schema or self.config.default_schema, name, "table", rel_path, line)
            if obj is None:
                return
        select = expression.copy()
        select.set("into", None)
        obj.definition = Statement("select_into", select, rel_path, line, owner=owner,
                                   reads=reads, target_key=obj.key)

    # -- object registry -----------------------------------------------------------

    def _define(self, schema: str, name: str, kind: str, rel_path: str, line: int) -> SqlObject | None:
        key = f"{schema}.{name}".lower()
        existing = self.objects.get(key)
        if existing is not None and existing.in_project:
            self.analysis.problem(
                "duplicates", f"{existing.label} ({existing.file}:{existing.line} / {rel_path}:{line})")
            return None
        obj = SqlObject(key=key, schema=schema, name=name, kind=kind, file=rel_path, line=line)
        self.objects[key] = obj
        return obj

    def _define_local(self, name: str, owner: str, kind: str, rel_path: str, line: int) -> SqlObject:
        key = _local_key(name, owner)
        obj = self.objects.get(key)
        if obj is None:
            obj = SqlObject(key=key, schema="", name=name, kind=kind, file=rel_path, line=line)
            self.objects[key] = obj
        return obj


def _parse(text: str) -> list[exp.Expression]:
    """
    The statements of `text`. Only when they can't be read as written, again
    without their OUTPUT clauses: stripping them up front would also cut a
    column that happens to be called Output.
    """
    try:
        return [e for e in sqlglot.parse(text, read=DIALECT) if e is not None]
    except SqlglotError:
        stripped = scanner.strip_output_clauses(text)
        if stripped == text:
            raise
        return [e for e in sqlglot.parse(stripped, read=DIALECT) if e is not None]


def _local_key(name: str, owner: str) -> str:
    return f"{name.lower()}@{owner.lower()}"


def _split_dotted(dotted: str, default_schema: str) -> tuple[str, str]:
    parts = []
    for part in dotted.split("."):
        text = part.strip()
        if len(text) >= 2 and text[0] in "[\"" and text[-1] in "]\"":
            text = text[1:-1]
        parts.append(text)
    if len(parts) == 1:
        return default_schema, parts[0]
    return parts[-2], parts[-1]


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:160]


# ---------------------------------------------------------------------------
# Pass 2: names, columns, edges
# ---------------------------------------------------------------------------

class _Resolver:

    def __init__(self, config: LineageConfig, analysis: Analysis) -> None:
        self.config = config
        self.analysis = analysis
        self.objects = analysis.objects
        self.by_name: dict[str, list[str]] = {}
        for key, obj in self.objects.items():
            if obj.in_project and obj.kind not in LOCAL_KINDS:
                self.by_name.setdefault(obj.name.lower(), []).append(key)
        self._columns: dict[str, list[str] | None] = {}
        self._computing: set[str] = set()
        self._prepared: dict[int, tuple] = {}
        self._reported: set[str] = set()

    # -- names ------------------------------------------------------------------------

    def resolve(self, table: exp.Table, statement: Statement, ctes: set[str]) -> str | None:
        local = _local_name(table)
        if local is not None:
            key = _local_key(local, statement.owner)
            if key not in self.objects:
                self.objects[key] = SqlObject(key=key, schema="", name=local,
                                              kind="table_variable" if local.startswith("@") else "temp")
            return key
        prefix, schema, name = _table_parts(table)
        if not name:
            return None
        if schema.lower() == scanner.FILE_SCHEMA:
            read = statement.reads.get(name.lower())
            if read is None:
                return None
            key = f"file:{read.path.lower()}"
            if key not in self.objects:
                self.objects[key] = SqlObject(key=key, schema="", name=read.path, kind="file",
                                              columns=read.columns, in_project=False,
                                              location=read.path)
            return key
        if not schema and not prefix and name.lower() in ctes:
            return None
        if schema:
            key = f"{schema}.{name}".lower()
            if not prefix or (key in self.objects and self.objects[key].in_project):
                return self._known(key, schema, name)
            # Another database or a linked server: outside the project.
            return self._known(f"{prefix}.{schema}.{name}".lower(), f"{prefix}.{schema}", name)
        default_key = f"{self.config.default_schema}.{name}".lower()
        if default_key in self.objects and self.objects[default_key].in_project:
            return default_key
        candidates = self.by_name.get(name.lower(), [])
        if len(candidates) == 1:
            return candidates[0]
        key = self._known(default_key, self.config.default_schema, name)
        if len(candidates) > 1 and key not in self._reported:
            self._reported.add(key)
            self.objects[key].flag = "ambiguous"
            self.analysis.problem(
                "ambiguous", f"{statement.evidence}: '{name}' without schema could be "
                f"{', '.join(self.objects[c].label for c in candidates)}")
        return key

    def _known(self, key: str, schema: str, name: str) -> str:
        if key not in self.objects:
            self.objects[key] = SqlObject(key=key, schema=schema, name=name, kind="unknown",
                                          in_project=False, flag="not in project")
        return key

    # -- columns of an object ------------------------------------------------------------

    def columns_of(self, key: str) -> list[str] | None:
        if key in self._columns:
            return self._columns[key]
        obj = self.objects.get(key)
        if obj is None or key in self._computing:
            return None
        self._computing.add(key)
        try:
            columns = obj.columns
            if columns is None and obj.definition is not None:
                columns = self._output_names(obj.definition)
            if columns is None:
                source = self.rule_source(obj)
                if source is not None and source[1] in self.objects:
                    columns = self.columns_of(source[1])
        finally:
            self._computing.discard(key)
        if columns is not None:
            obj.columns = list(columns)
        self._columns[key] = columns
        return columns

    def rule_source(self, obj: SqlObject):
        """(rule, source key or external label) of the first rule matching `obj`, or None."""
        if not obj.in_project or obj.kind in LOCAL_KINDS or obj.kind == "file":
            return None
        for rule in self.config.rules:
            source = rule.source_for(obj.schema, obj.name)
            if source is None:
                continue
            if rule.external:
                return rule, f"source:{source.lower()}", source
            parts = [p.strip().strip("[]") for p in source.split(".")]
            key = ".".join(parts).lower() if len(parts) == 2 else ""
            return rule, key, source
        return None

    # -- statements ----------------------------------------------------------------------

    def run(self, statements: list[Statement]) -> None:
        for statement in statements:
            self.analysis.statements += 1
            try:
                self._statement(statement)
            except Exception as exc:  # noqa: BLE001 - one statement must not stop the project
                self.analysis.problem("column_errors",
                                      f"{statement.evidence}: {type(exc).__name__}: {_short(exc)}")
        self._external_tables()
        self._computed_columns()
        self._rules()
        self._collapse_locals()

    def _target(self, statement: Statement) -> str | None:
        if statement.target_key:
            return statement.target_key
        expression = statement.expression
        ctes = _cte_names(expression)
        if statement.kind == "insert":
            target = expression.this
            target = target.this if isinstance(target, exp.Schema) else target
        elif statement.kind == "merge":
            target = expression.this
        elif statement.kind == "select_into":
            target = expression.args["into"].this
        elif statement.kind == "update":
            target = self._update_target(expression)
        else:
            return None
        if not isinstance(target, exp.Table):
            return None
        statement.target_key = self.resolve(target, statement, ctes) or ""
        return statement.target_key or None

    def _update_target(self, update: exp.Update) -> exp.Table | None:
        """UPDATE x SET ... FROM core.t AS x: the target is the table aliased 'x'."""
        target = update.this
        if not isinstance(target, exp.Table) or target.args.get("db"):
            return target
        alias = target.name.lower()
        for table in _from_tables(update):
            if table.alias_or_name.lower() == alias:
                return table
        return target

    def _statement(self, statement: Statement) -> None:
        target = self._target(statement)
        if target is None:
            return
        query, target_columns = self._column_query(statement, target)
        expression = statement.expression
        ctes = _cte_names(expression)
        target_node = _target_node(statement)
        for table in expression.find_all(exp.Table):
            if table is target_node:
                continue
            source = self.resolve(table, statement, ctes)
            if source is not None and source != target:
                self._edge(self.analysis.object_edges, source, target, statement)
        if query is None:
            return
        prepared = self._prepare(statement, query)
        if prepared is None:
            return
        scope, schema, mapping, names = prepared
        spelling = _spellings(query)
        if "*" in names or not target_columns:
            self.analysis.problem(
                "unknown_columns",
                f"{statement.evidence}: columns of {self.objects[target].label} not determined"
                + (" (SELECT * over an object whose columns are unknown)" if "*" in names else ""))
            return
        for index, column in enumerate(target_columns):
            if index >= len(names) or column is None:
                continue
            try:
                node = to_node(index, scope, dialect=DIALECT, schema=schema, trim_selects=False)
            except Exception as exc:  # noqa: BLE001 - the other columns still count
                self.analysis.problem("column_errors", f"{statement.evidence}: {column}: {_short(exc)}")
                continue
            for leaf in node.walk():
                if leaf.downstream or not isinstance(leaf.source, exp.Table):
                    continue
                source_key = mapping.get((leaf.source.args.get("db") and leaf.source.db.lower(),
                                          leaf.source.name.lower()))
                if source_key is None:
                    continue
                source_column = self._spelled(source_key, leaf.name.split(".")[-1].strip('"[]`'), spelling)
                if source_key == target and source_column.lower() == column.lower():
                    continue
                self._edge(self.analysis.column_edges, column_key(source_key, source_column),
                           column_key(target, column), statement)

    def _column_query(self, statement: Statement, target: str):
        """(the query whose outputs feed the target, the target column of each output)."""
        expression = statement.expression
        if statement.kind in ("view", "function", "create_as"):
            names = statement.column_names or self.columns_of(target)
            return expression, names
        if statement.kind == "select_into":
            select = expression.copy()
            select.set("into", None)
            return select, self.columns_of(target)
        if statement.kind == "insert":
            query = _first_query(expression.args.get("expression"))
            if query is None:
                return None, None              # INSERT ... VALUES: constants
            if expression.args.get("with_") or expression.args.get("with"):
                query = query.copy()
                _keep_ctes(expression, query)
            schema = expression.this
            if isinstance(schema, exp.Schema) and schema.expressions:
                return query, [_identifier(c) or c.name for c in schema.expressions]
            return query, self.columns_of(target)
        if statement.kind == "merge":
            return _merge_query(expression)
        if statement.kind == "update":
            return _update_query(expression)
        return None, None

    def _prepare(self, statement: Statement, query: exp.Expression):
        """
        The query qualified against the catalog: (scope, schema, {(db, table): key}, outputs).

        Every table gets a synthetic name before qualification: the catalog
        can then hold temporary tables, files and unqualified names that the
        parser would otherwise attribute to no schema, and a self-join keeps
        two distinct sources.
        """
        cached = self._prepared.get(id(query))
        if cached is not None and cached[0] is query:
            return cached[1]
        copy = query.copy()
        ctes = _cte_names(copy)
        spelling = _spellings(query)
        tables: dict[str, dict[str, dict[str, str]]] = {}
        mapping: dict[tuple, str] = {}
        for index, table in enumerate(list(copy.find_all(exp.Table))):
            key = self.resolve(table, statement, ctes)
            if key is None:
                continue
            synthetic_db, synthetic_name = f"s{index}", f"t{index}"
            if not table.args.get("alias"):
                table.set("alias", exp.TableAlias(this=exp.to_identifier(
                    (_local_name(table) or table.name).lstrip("#@") or synthetic_name)))
            table.set("this", exp.to_identifier(synthetic_name))
            table.set("db", exp.to_identifier(synthetic_db))
            table.set("catalog", None)
            mapping[(synthetic_db, synthetic_name)] = key
            columns = self.columns_of(key)
            if columns:
                tables.setdefault(synthetic_db, {})[synthetic_name] = {c.lower(): "unknown" for c in columns}
                for column in columns:          # the spelling of columns a '*' expands to
                    spelling.setdefault(column.lower(), column)
        schema = MappingSchema(tables, dialect=DIALECT)
        try:
            qualified = qualify(copy, schema=schema, dialect=DIALECT, validate_qualify_columns=False,
                                identify=False, quote_identifiers=False)
            scope = build_scope(qualified)
        except Exception as exc:  # noqa: BLE001 - the object edges still count
            self.analysis.problem("column_errors", f"{statement.evidence}: {_short(exc)}")
            self._prepared[id(query)] = (query, None)
            return None
        if scope is None:
            self._prepared[id(query)] = (query, None)
            return None
        names = list(qualified.named_selects) if hasattr(qualified, "named_selects") else []
        outputs = [spelling.get(n.lower(), n) for n in names]
        result = (scope, schema, mapping, outputs)
        self._prepared[id(query)] = (query, result)
        return result

    def _output_names(self, statement: Statement) -> list[str] | None:
        prepared = self._prepare(statement, statement.expression)
        if prepared is None:
            return None
        names = prepared[3]
        if not names or "*" in names:
            return None
        return statement.column_names or names

    def _spelled(self, key: str, column: str, spelling: dict | None = None) -> str:
        """The column as declared by its object, else as written in the statement."""
        for declared in self.columns_of(key) or []:
            if declared.lower() == column.lower():
                return declared
        return (spelling or {}).get(column.lower(), column)

    def _edge(self, edges: list, source: str, target: str, statement: Statement) -> None:
        edges.append(Edge(source, target, statement.kind, statement.evidence))

    # -- edges that are not statements -----------------------------------------------------

    def _external_tables(self) -> None:
        """An external table reads the files at its LOCATION, column by column."""
        for obj in list(self.objects.values()):
            if obj.kind != "external_table" or not obj.location:
                continue
            key = f"file:{obj.location.lower()}"
            if key not in self.objects:
                self.objects[key] = SqlObject(key=key, schema="", name=obj.location, kind="file",
                                              columns=obj.columns, in_project=False,
                                              location=obj.location)
            evidence = f"{obj.file}:{obj.line}"
            writes = obj.definition is not None         # CREATE EXTERNAL TABLE AS SELECT
            source, target = (obj.key, key) if writes else (key, obj.key)
            self.analysis.object_edges.append(Edge(source, target, "location", evidence))
            for column in self.columns_of(obj.key) or []:
                self.analysis.column_edges.append(
                    Edge(column_key(source, column), column_key(target, column), "location", evidence))

    def _computed_columns(self) -> None:
        for obj in list(self.objects.values()):
            for column, sources in obj.computed.items():
                for source in sources:
                    self.analysis.column_edges.append(
                        Edge(column_key(obj.key, self._spelled(obj.key, source)),
                             column_key(obj.key, column), "computed", f"{obj.file}:{obj.line}"))

    def _rules(self) -> None:
        report = self.analysis.rules
        for rule in self.config.rules:
            report[rule.name] = {"matched": [], "found": 0, "missing": [], "external": rule.external}
        for obj in sorted(self.objects.values(), key=lambda o: o.key):
            if self.config.is_ignored(obj.schema):
                continue
            found = self.rule_source(obj)
            if found is None:
                continue
            rule, source_key, source_text = found
            entry = report[rule.name]
            entry["matched"].append(obj.label)
            evidence = f"rule '{rule.name}'"
            if rule.external:
                if source_key not in self.objects:
                    schema, _dot, name = source_text.rpartition(".")
                    self.objects[source_key] = SqlObject(
                        key=source_key, schema=schema, name=name or source_text, kind="source",
                        in_project=False)
                entry["found"] += 1
                source_columns = None
            elif source_key and source_key in self.objects and self.objects[source_key].in_project:
                entry["found"] += 1
                source_columns = self.columns_of(source_key)
            else:
                entry["missing"].append(f"{obj.label} -> {source_text}")
                continue
            self.analysis.object_edges.append(Edge(source_key, obj.key, "rule", evidence, rule.name))
            available = {c.lower(): c for c in source_columns or []}
            for column in self.columns_of(obj.key) or []:
                wanted = rule.columns.get(column.lower(), column)
                if rule.external:
                    source_column = wanted
                    columns = self.objects[source_key].columns or []
                    if source_column not in columns:
                        columns.append(source_column)
                        self.objects[source_key].columns = columns
                elif wanted.lower() in available:
                    source_column = available[wanted.lower()]
                else:
                    continue
                self.analysis.column_edges.append(
                    Edge(column_key(source_key, source_column), column_key(obj.key, column),
                         "rule", evidence, rule.name))

    def _collapse_locals(self) -> None:
        """source -> #t -> target becomes source -> target; temporary objects disappear."""
        locals_ = {k for k, o in self.objects.items() if o.kind in LOCAL_KINDS}
        self.analysis.object_edges = _collapse(self.analysis.object_edges,
                                               lambda node: node in locals_)
        self.analysis.column_edges = _collapse(self.analysis.column_edges,
                                               lambda node: _object_of(node) in locals_)
        for key in locals_:
            self.objects.pop(key, None)


def _collapse(edges: list, is_local) -> list:
    incoming: dict[str, set] = {}
    outgoing: dict[str, set] = {}
    alive = set(edges)
    for edge in edges:
        outgoing.setdefault(edge.source, set()).add(edge)
        incoming.setdefault(edge.target, set()).add(edge)
    for node in sorted({n for e in edges for n in (e.source, e.target) if is_local(n)}):
        ins = [e for e in incoming.pop(node, set()) if e.source != node]
        outs = [e for e in outgoing.pop(node, set()) if e.target != node]
        for edge in ins:
            outgoing.get(edge.source, set()).discard(edge)
            alive.discard(edge)
        for edge in outs:
            incoming.get(edge.target, set()).discard(edge)
            alive.discard(edge)
        for into in ins:
            for out in outs:
                if into.source == out.target:
                    continue
                composed = Edge(into.source, out.target, out.kind, out.evidence,
                                out.rule or into.rule)
                alive.add(composed)
                outgoing.setdefault(composed.source, set()).add(composed)
                incoming.setdefault(composed.target, set()).add(composed)
    order = {id(e): i for i, e in enumerate(edges)}
    return sorted((e for e in alive if not is_local(e.source) and not is_local(e.target)),
                  key=lambda e: (order.get(id(e), len(order)), e.source, e.target))


def _object_of(node_key: str) -> str:
    return node_key.split("|", 1)[0]


def _cte_names(expression: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE) if cte.alias_or_name}


def _target_node(statement: Statement):
    expression = statement.expression
    if statement.kind == "insert":
        target = expression.this
        return target.this if isinstance(target, exp.Schema) else target
    if statement.kind == "merge":
        return expression.this
    if statement.kind == "select_into":
        return expression.args["into"].this
    if statement.kind == "update":
        return expression.this
    return None


def _from_tables(expression) -> list[exp.Table]:
    tables = []
    from_ = expression.args.get("from_") or expression.args.get("from")
    if from_ is not None:
        tables.extend(t for t in from_.find_all(exp.Table))
    for join in expression.args.get("joins") or []:
        tables.extend(t for t in join.find_all(exp.Table))
    return tables


def _merge_query(merge: exp.Merge):
    """MERGE as a SELECT of the values it writes, FROM its source joined to its target."""
    pairs: list[tuple[str, exp.Expression]] = []
    whens = merge.args.get("whens")
    for when in (whens.expressions if whens is not None else []):
        then = when.args.get("then")
        if isinstance(then, exp.Update):
            for assignment in then.expressions:
                if isinstance(assignment, exp.EQ) and isinstance(assignment.this, exp.Column):
                    pairs.append((assignment.this.name, assignment.expression))
        elif isinstance(then, exp.Insert):
            columns, values = then.this, then.expression
            if isinstance(columns, (exp.Tuple, exp.Schema)) and isinstance(values, exp.Tuple):
                for column, value in zip(columns.expressions, values.expressions, strict=False):
                    pairs.append((_identifier(column) or column.name, value))
    using = merge.args.get("using")
    if not pairs or using is None:
        return None, None
    select = exp.Select(expressions=[value.copy() for _name, value in pairs])
    select.set("from_", exp.From(this=using.copy()))
    select.set("joins", [exp.Join(this=merge.this.copy(), kind="CROSS")])
    _keep_ctes(merge, select)
    return select, [name for name, _value in pairs]


def _update_query(update: exp.Update):
    """UPDATE as a SELECT of the values it writes, FROM its FROM clause (or its target)."""
    pairs = [(a.this.name, a.expression) for a in update.expressions
             if isinstance(a, exp.EQ) and isinstance(a.this, exp.Column)]
    if not pairs:
        return None, None
    select = exp.Select(expressions=[value.copy() for _name, value in pairs])
    from_ = update.args.get("from_") or update.args.get("from")
    if from_ is not None:
        select.set("from_", from_.copy())
        joins = update.args.get("joins")
        if joins:
            select.set("joins", [j.copy() for j in joins])
    else:
        select.set("from_", exp.From(this=update.this.copy()))
    _keep_ctes(update, select)
    return select, [name for name, _value in pairs]


def _keep_ctes(statement: exp.Expression, query: exp.Expression) -> None:
    """
    The WITH of an INSERT, UPDATE or MERGE goes to the query standing for its
    values: its CTEs are not tables (lost, they looked like objects outside
    the project, and the tables they read were not sources).
    """
    with_ = statement.args.get("with_") or statement.args.get("with")
    key = "with_" if "with_" in type(query).arg_types else "with"
    if with_ is not None and key in type(query).arg_types and query.args.get(key) is None:
        query.set(key, with_.copy())


def _spellings(expression: exp.Expression) -> dict[str, str]:
    """lowercase -> spelling as written, for the identifiers of a statement."""
    spelling: dict[str, str] = {}
    for identifier in expression.find_all(exp.Identifier):
        text = identifier.this
        if isinstance(text, str):
            spelling.setdefault(text.lower(), text)
    return spelling


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def analyze(files: list[SqlFile], config: LineageConfig) -> Analysis:
    analysis = Analysis(objects={}, object_edges=[], column_edges=[])
    statements = _Scanner(config, analysis).scan(files)
    _Resolver(config, analysis).run(statements)
    analysis.object_edges = _dedupe(analysis.object_edges)
    analysis.column_edges = _dedupe(analysis.column_edges)
    return analysis


def query_lineage(sql: str, analysis: Analysis | None, config: LineageConfig, evidence: str) -> dict:
    """
    Lineage of one SELECT run from outside the project (a Power Query native
    query), against the catalog of an analyzed project, or of none.

    {"objects": {key: SqlObject} the objects it reads,
     "columns": {output column: [(object key, column)]},
     "problems": [text]}

    The project's analysis is shared (cached): the objects that only this
    query references live in a copy of the catalog, never in the analysis.
    """
    scratch = Analysis(objects=dict(analysis.objects) if analysis else {},
                       object_edges=[], column_edges=[])
    result: dict = {"objects": {}, "columns": {}, "problems": []}
    try:
        parsed = [e for e in _parse(sql) if not isinstance(e, exp.Command)]
    except SqlglotError as exc:
        result["problems"].append(f"{evidence}: the native query can't be read ({_short(exc)})")
        return result
    query = _first_query(parsed[0]) if parsed else None
    if query is None:
        result["problems"].append(f"{evidence}: the native query is not a SELECT")
        return result
    normalize_applies(query)
    resolver = _Resolver(config, scratch)
    statement = Statement("query", query, evidence, 0, owner=evidence)
    ctes = _cte_names(query)
    try:
        for table in query.find_all(exp.Table):
            key = resolver.resolve(table, statement, ctes)
            if key is not None:
                result["objects"][key] = scratch.objects[key]
        prepared = resolver._prepare(statement, query)
        spelling = _spellings(query)
        if prepared is not None:
            scope, schema, mapping, names = prepared
            for index, name in enumerate(names):
                if name == "*":
                    continue
                node = to_node(index, scope, dialect=DIALECT, schema=schema, trim_selects=False)
                sources = []
                for leaf in node.walk():
                    if leaf.downstream or not isinstance(leaf.source, exp.Table):
                        continue
                    key = mapping.get((leaf.source.args.get("db") and leaf.source.db.lower(),
                                       leaf.source.name.lower()))
                    if key is not None:
                        sources.append((key, resolver._spelled(
                            key, leaf.name.split(".")[-1].strip('"[]`'), spelling)))
                result["columns"][name] = sources
    except Exception as exc:  # noqa: BLE001 - a native query must not stop the model's lineage
        result["problems"].append(f"{evidence}: {type(exc).__name__}: {_short(exc)}")
    result["problems"].extend(p for items in scratch.problems.values() for p in items)
    return result


def _dedupe(edges: list[Edge]) -> list[Edge]:
    seen: dict[tuple, Edge] = {}
    for edge in edges:
        if edge.source == edge.target:
            continue
        seen.setdefault((edge.source, edge.target, edge.kind), edge)
    return list(seen.values())


def relative(path: str, folder: str) -> str:
    return os.path.relpath(path, folder).replace("\\", "/")
