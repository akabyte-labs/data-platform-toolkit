"""
Where the tables of a semantic model get their data, column by column: the
Power Query (M) of their partitions and Direct Lake entities.

M is a whole functional language. This reads what lineage needs, in the
forms Power BI Desktop writes and the common hand-written ones:

  - the source: `Sql.Database` / `Sql.Databases` with the navigation to a
    schema and an object, a native query (`Value.NativeQuery`, the `Query`
    option), any other connector by name (Excel, SharePoint, a lakehouse...),
    a Direct Lake entity;
  - the steps between the source and the table, followed backwards for each
    column: renames, added and duplicated columns, groupings, merges
    (`Table.NestedJoin` + `Table.ExpandTableColumn`), appends
    (`Table.Combine`), and the steps that only filter, sort or set types;
  - references to other queries, parameters, and query functions called
    with literal arguments.

Anything else stops the trace at the step that can't be read, and the
reason says which step: never a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.model_loader import load_definition

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Origin:
    """Where one model column comes from (one of possibly several origins)."""
    kind: str                   # db, native, external, generated, unresolved
    schema: str = ""
    name: str = ""              # db object, or external item
    column: str = ""
    server: str = ""
    database: str = ""
    connector: str = ""
    query: str = ""             # the SQL of a native query
    how: tuple = ()             # steps on the way, from the source to the table
    reason: str = ""            # why it is unresolved, or what generated it


@dataclass
class TableSource:
    """One partition of a table and where each of its columns comes from."""
    table: str
    partition: str
    kind: str                   # m, direct_lake, calculated, calculation_group, other
    mode: str
    file: str
    line: int
    dax: str = ""               # the DAX of a calculated partition
    columns: dict = field(default_factory=dict)         # model column -> [Origin]

    def objects(self) -> list[Origin]:
        """The database objects and other sources the partition reads (one Origin each)."""
        seen: dict[tuple, Origin] = {}
        for origins in self.columns.values():
            for origin in origins:
                if origin.kind in ("db", "native", "external"):
                    key = (origin.kind, origin.schema.lower(), origin.name.lower(), origin.query)
                    seen.setdefault(key, replace(origin, column="", how=()))
        return list(seen.values())


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Tok:
    kind: str                   # ident, string, number, punct
    text: str                   # identifiers unquoted, strings unescaped
    start: int
    end: int


_PUNCT2 = ("=>", "<=", ">=", "<>", "..", "??")
_ESCAPE_RE = re.compile(r"#\(([^)]*)\)")
_ESCAPES = {"lf": "\n", "cr": "\r", "tab": "\t", "#": "#"}


def _unescape(value: str) -> str:
    """M string escapes: '#(lf)', '#(cr,lf)', '#(tab)', '#(#)', '#(000D)'."""
    def one(match: re.Match) -> str:
        out = []
        for part in match.group(1).split(","):
            part = part.strip()
            if part.lower() in _ESCAPES:
                out.append(_ESCAPES[part.lower()])
            elif re.fullmatch(r"[0-9A-Fa-f]{4}|[0-9A-Fa-f]{8}", part):
                out.append(chr(int(part, 16)))
            else:
                return match.group(0)
        return "".join(out)
    return _ESCAPE_RE.sub(one, value)


def _tokenize(text: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char.isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif char == '"' or text.startswith('#"', i):
            quoted = char == "#"
            j = i + (2 if quoted else 1)
            chars = []
            while j < n:
                if text[j] == '"':
                    if text[j + 1:j + 2] == '"':
                        chars.append('"')
                        j += 2
                        continue
                    j += 1
                    break
                chars.append(text[j])
                j += 1
            value = "".join(chars)
            tokens.append(_Tok("ident", value, i, j) if quoted else _Tok("string", _unescape(value), i, j))
            i = j
        elif char.isalpha() or char in "_#":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_."):
                j += 1
            while j > i + 1 and text[j - 1] == ".":
                j -= 1
            tokens.append(_Tok("ident", text[i:j], i, j))
            i = j
        elif char.isdigit():
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "."):
                j += 1
            tokens.append(_Tok("number", text[i:j], i, j))
            i = j
        elif text[i:i + 2] in _PUNCT2:
            tokens.append(_Tok("punct", text[i:i + 2], i, i + 2))
            i += 2
        else:
            tokens.append(_Tok("punct", char, i, i + 1))
            i += 1
    return tokens


_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {")", "]", "}"}


def _is(token: _Tok | None, text: str) -> bool:
    return token is not None and token.kind == "punct" and token.text == text


def _word(token: _Tok | None, text: str) -> bool:
    return token is not None and token.kind == "ident" and token.text == text


def _closing(tokens: list[_Tok], index: int) -> int:
    """Index of the token closing tokens[index] (an opener), or len(tokens)."""
    depth = 0
    for i in range(index, len(tokens)):
        token = tokens[i]
        if token.kind != "punct":
            continue
        if token.text in _OPENERS:
            depth += 1
        elif token.text in _CLOSERS:
            depth -= 1
            if depth == 0:
                return i
    return len(tokens)


def _split(tokens: list[_Tok], separator: str = ",") -> list[list[_Tok]]:
    """Parts separated at bracket depth zero."""
    parts, current, depth = [], [], 0
    for token in tokens:
        if token.kind == "punct":
            if token.text in _OPENERS:
                depth += 1
            elif token.text in _CLOSERS:
                depth -= 1
            elif token.text == separator and depth == 0:
                parts.append(current)
                current = []
                continue
        current.append(token)
    if current or parts:
        parts.append(current)
    return parts


def _strip(tokens: list[_Tok]) -> list[_Tok]:
    """Without the parentheses that wrap the whole expression."""
    while len(tokens) >= 2 and _is(tokens[0], "(") and _closing(tokens, 0) == len(tokens) - 1:
        tokens = tokens[1:-1]
    return tokens


def _inner(tokens: list[_Tok]) -> list[_Tok] | None:
    """The content of a list `{...}` or record `[...]` that is the whole expression."""
    tokens = _strip(tokens)
    if len(tokens) >= 2 and tokens[0].kind == "punct" and tokens[0].text in ("{", "[") \
            and _closing(tokens, 0) == len(tokens) - 1:
        return tokens[1:-1]
    return None


def _call(tokens: list[_Tok]) -> tuple[str, list[list[_Tok]], list[_Tok]] | None:
    """(function, arguments, what follows the call) of `F(...)...`, or None."""
    tokens = _strip(tokens)
    if len(tokens) >= 3 and tokens[0].kind == "ident" and _is(tokens[1], "("):
        end = _closing(tokens, 1)
        inside = tokens[2:end]
        args = _split(inside) if inside else []
        return tokens[0].text, args, tokens[end + 1:]
    return None


def _record(tokens: list[_Tok]) -> dict[str, list[_Tok]] | None:
    """`[Key = value, ...]` as {key: value tokens}, or None."""
    inner = _inner(tokens)
    if inner is None or not _is(_strip(tokens)[0], "["):
        return None
    result = {}
    for part in _split(inner):
        if len(part) >= 2 and part[0].kind == "ident" and _is(part[1], "="):
            result[part[0].text] = part[2:]
    return result


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

@dataclass
class _Query:
    name: str
    steps: dict                 # step name -> tokens, in order
    result: list
    params: tuple = ()          # a query function: (params) => ...
    text: str = ""              # the M the tokens point into


def _parse_query(name: str, text: str) -> _Query:
    tokens = _tokenize(text or "")
    params: tuple = ()
    if tokens and _is(tokens[0], "("):
        end = _closing(tokens, 0)
        arrow = next((i for i in range(end + 1, min(end + 5, len(tokens))) if _is(tokens[i], "=>")), None)
        if arrow is not None:
            names = []
            for part in _split(tokens[1:end]):
                idents = [t.text for t in part if t.kind == "ident" and t.text != "optional"]
                if idents:
                    names.append(idents[0])
            params = tuple(names)
            tokens = tokens[arrow + 1:]
    steps, result = _parse_let(tokens)
    return _Query(name, steps, result, params, text or "")


def _parse_let(tokens: list[_Tok]) -> tuple[dict, list]:
    if not tokens or not _word(tokens[0], "let"):
        return {}, tokens
    steps: dict[str, list[_Tok]] = {}
    i = 1
    while i < len(tokens):
        if not (tokens[i].kind == "ident" and i + 1 < len(tokens) and _is(tokens[i + 1], "=")):
            return steps, []
        name = tokens[i].text
        j = start = i + 2
        depth = lets = 0
        while j < len(tokens):
            token = tokens[j]
            if token.kind == "punct" and token.text in _OPENERS:
                depth += 1
            elif token.kind == "punct" and token.text in _CLOSERS:
                depth -= 1
            elif depth == 0 and _word(token, "let"):
                lets += 1
            elif depth == 0 and _word(token, "in"):
                if lets == 0:
                    break
                lets -= 1
            elif depth == 0 and lets == 0 and _is(token, ","):
                break
            j += 1
        steps[name] = tokens[start:j]
        if j >= len(tokens):
            return steps, []
        if _is(tokens[j], ","):
            i = j + 1
            continue
        return steps, tokens[j + 1:]
    return steps, []


def _string_list(tokens: list[_Tok]) -> list[str] | None:
    """`{"a", "b"}` or `"a"` as a list of strings."""
    tokens = _strip(tokens)
    if len(tokens) == 1 and tokens[0].kind == "string":
        return [tokens[0].text]
    inner = _inner(tokens)
    if inner is None:
        return None
    values = []
    for part in _split(inner):
        part = _strip(part)
        if len(part) == 1 and part[0].kind == "string":
            values.append(part[0].text)
        else:
            return None
    return values


def _pairs(tokens: list[_Tok]) -> list[tuple[str, str]]:
    """`{{"a", "b"}, {"c", "d"}}` or `{"a", "b"}`: (old, new) pairs."""
    single = _string_list(tokens)
    if single is not None and len(single) == 2:
        return [(single[0], single[1])]
    inner = _inner(tokens) or []
    pairs = []
    for part in _split(inner):
        values = _string_list(part)
        if values is not None and len(values) >= 2:
            pairs.append((values[0], values[1]))
    return pairs


# Functions that keep the columns of their first argument as they are:
# they filter, sort, type, replace values, or add nothing.
_KEEPS_COLUMNS = {
    "Table.TransformColumnTypes", "Table.SelectColumns", "Table.RemoveColumns",
    "Table.ReorderColumns", "Table.SelectRows", "Table.Sort", "Table.Distinct", "Table.FirstN",
    "Table.LastN", "Table.Skip", "Table.RemoveFirstN", "Table.RemoveLastN", "Table.Buffer",
    "Table.TransformColumns", "Table.ReplaceValue", "Table.ReplaceErrors",
    "Table.RemoveRowsWithErrors", "Table.SelectRowsWithErrors", "Table.FillDown", "Table.FillUp",
    "Table.PromoteHeaders", "Table.RemoveMatchingRows", "Table.Range", "Table.AlternateRows",
    "Table.ReverseRows", "Table.AddKey", "Table.ReplaceKeys", "Table.Repeat", "Table.InsertRows",
    "Table.RemoveRows", "Table.ReplaceRows", "Table.StopFolding", "Table.ConformToPageReader",
    "Table.Profile", "Table.MaxN", "Table.MinN", "Table.Partition", "Table.View",
}
_STATIC_TABLES = {"#table", "Table.FromRows", "Table.FromRecords", "Table.FromList",
                  "Table.FromColumns", "Table.FromValue"}
_RESHAPES = {"Table.Pivot", "Table.Unpivot", "Table.UnpivotOtherColumns", "Table.Transpose",
             "Table.TransformColumnNames", "Table.Join", "Table.FuzzyJoin", "Table.FuzzyNestedJoin",
             "Table.AddJoinColumn"}
_SQL_SOURCES = {"Sql.Database", "Sql.Databases"}
_MAX_DEPTH = 80


def _where(step: str) -> str:
    return f"step '{step}'" if step else "the query"


def _unresolved(reason: str) -> list[Origin]:
    return [Origin("unresolved", reason=reason)]


def _with_how(origins: list[Origin], step: str) -> list[Origin]:
    return [replace(o, how=o.how + (step,)) if o.kind not in ("unresolved",) else o for o in origins]


@dataclass
class _Source:
    """A data source value and the navigation applied to it so far."""
    connector: str
    server: str = ""
    database: str = ""
    query: str | None = None
    path: tuple = ()            # names navigated through, for other connectors


class _Tracer:

    def __init__(self, queries: dict[str, _Query]) -> None:
        self.queries = queries
        self.by_lower = {name.lower(): q for name, q in queries.items()}

    def query(self, name: str) -> _Query | None:
        return self.queries.get(name) or self.by_lower.get(name.lower())

    # -- tables ----------------------------------------------------------------------

    def column(self, query: _Query, column: str) -> list[Origin]:
        return self._table(query, query.result, column, {}, "", 0)

    def _table(self, query, tokens, column, env, step, depth) -> list[Origin]:
        if depth > _MAX_DEPTH:
            return _unresolved("too many steps to follow")
        tokens = _strip(tokens)
        if not tokens:
            return _unresolved(f"{_where(step)} is empty")
        if len(tokens) == 1 and tokens[0].kind == "ident":
            return self._reference(query, tokens[0].text, column, env, depth)
        if _word(tokens[0], "let"):
            steps, result = _parse_let(tokens)
            inner = _Query(query.name, {**query.steps, **steps}, result, query.params, query.text)
            return self._table(inner, result, column, env, step, depth + 1)

        called = _call(tokens)
        primary_end = 1 if called is None else len(tokens) - len(called[2])
        postfix = tokens[primary_end:]
        if postfix and postfix[0].kind == "punct" and postfix[0].text in ("{", "["):
            source = self._source(query, tokens[:primary_end], env, depth + 1)
            if source is None:
                return _unresolved(f"{_where(step)}: navigation not interpreted")
            return [self._navigate(source, postfix, column, query, env)]
        if called is None:
            return _unresolved(f"{_where(step)}: expression not interpreted")
        name, args, _rest = called
        return self._function(query, name, args, column, env, step, depth)

    def _reference(self, query, name, column, env, depth) -> list[Origin]:
        if name in query.steps:
            return self._table(query, query.steps[name], column, env, name, depth + 1)
        other = self.query(name)
        if other is not None and other is not query:
            if other.params:
                return _unresolved(f"'{name}' is a function, not a table")
            return self._table(other, other.result, column, {}, "", depth + 1)
        return _unresolved(f"'{name}' is neither a step nor a query of the model")

    def _function(self, query, name, args, column, env, step, depth) -> list[Origin]:
        def table(tokens, wanted=column):
            return self._table(query, tokens, wanted, env, step, depth + 1)

        def arg(index):
            return args[index] if index < len(args) else []

        if name == "Value.NativeQuery":
            source = self._source(query, arg(0), env, depth + 1)
            sql = self._string(query, arg(1), env, depth + 1)
            if sql is None:
                return _unresolved(f"{_where(step)}: the native query is built at refresh time")
            return [Origin("native", column=column, query=sql, connector="Value.NativeQuery",
                           server=source.server if source else "",
                           database=source.database if source else "")]
        if name in _SQL_SOURCES:
            source = self._source(query, _rebuild(name, args), env, depth + 1)
            if source is not None and source.query is not None:
                return [Origin("native", column=column, query=source.query, connector=name,
                               server=source.server, database=source.database)]
            return _unresolved(f"{_where(step)}: {name} without the navigation to a table")
        if name in _STATIC_TABLES:
            return [Origin("generated", column=column, reason=f"data written in {_where(step)}")]
        if name in _KEEPS_COLUMNS:
            return table(arg(0))
        if name == "Table.RenameColumns":
            for old, new in _pairs(arg(1)):
                if new == column:
                    return _with_how(table(arg(0), old), f"renamed from '{old}' in '{step}'")
            return table(arg(0))
        if name == "Table.AddColumn":
            if self._string(query, arg(1), env, depth + 1) == column:
                return self._derived(query, arg(0), arg(2), column, env, step, depth, "computed")
            return table(arg(0))
        if name == "Table.DuplicateColumn":
            if self._string(query, arg(2), env, depth + 1) == column:
                original = self._string(query, arg(1), env, depth + 1) or ""
                return _with_how(table(arg(0), original), f"copied from '{original}' in '{step}'")
            return table(arg(0))
        if name == "Table.AddIndexColumn":
            if self._string(query, arg(1), env, depth + 1) == column:
                return [Origin("generated", column=column, reason=f"index added in {_where(step)}")]
            return table(arg(0))
        if name == "Table.Group":
            keys = _string_list(arg(1)) or []
            if column in keys:
                return table(arg(0))
            aggregations = _inner(arg(2)) or []
            parts = _split(aggregations) if aggregations and _is(_strip(aggregations)[0], "{") else [arg(2)]
            for part in parts:
                values = _split(_inner(part) or [])
                if values and self._string(query, values[0], env, depth + 1) == column:
                    fn = values[1] if len(values) > 1 else []
                    return self._derived(query, arg(0), fn, column, env, step, depth, "aggregated")
            return _unresolved(f"{_where(step)}: '{column}' is not a key or an aggregation of the grouping")
        if name == "Table.NestedJoin":
            if self._string(query, arg(4), env, depth + 1) == column:
                return _unresolved(f"{_where(step)}: '{column}' is a nested table, not a column")
            return table(arg(0))
        if name == "Table.ExpandTableColumn":
            nested = self._string(query, arg(1), env, depth + 1) or ""
            names = _string_list(arg(2)) or []
            renamed = _string_list(arg(3)) or names
            if column in renamed and renamed.index(column) < len(names):
                original = names[renamed.index(column)]
                other = self._joined(query, arg(0), nested, env, depth + 1)
                if other is None:
                    return _unresolved(f"{_where(step)}: the merge that created '{nested}' was not found")
                return _with_how(table(other, original), f"merged in '{step}'")
            return table(arg(0))
        if name == "Table.ExpandRecordColumn":
            record = self._string(query, arg(1), env, depth + 1) or ""
            names = _string_list(arg(2)) or []
            renamed = _string_list(arg(3)) or names
            if column in renamed:
                return _with_how(table(arg(0), record), f"expanded from the record '{record}' in '{step}'")
            return table(arg(0))
        if name == "Table.SplitColumn":
            original = self._string(query, arg(1), env, depth + 1) or ""
            names = _string_list(arg(3)) or []
            if column in names or column.startswith(f"{original}."):
                return _with_how(table(arg(0), original), f"split from '{original}' in '{step}'")
            return table(arg(0))
        if name == "Table.CombineColumns":
            sources = _string_list(arg(1)) or []
            if self._string(query, arg(3), env, depth + 1) == column:
                origins = []
                for source in sources:
                    origins.extend(table(arg(0), source))
                return _with_how(origins, f"combined in '{step}'")
            return table(arg(0))
        if name == "Table.Combine":
            origins = []
            for part in _split(_inner(arg(0)) or []):
                origins.extend(table(part))
            return _with_how(origins, f"appended in '{step}'") if origins else \
                _unresolved(f"{_where(step)}: the appended tables were not found")
        if name in _RESHAPES:
            return _unresolved(f"{_where(step)}: {name} reshapes the table, its columns are not followed")

        function = self.query(name)
        if function is not None and function.params:
            values = {}
            for param, value in zip(function.params, args, strict=False):
                text = self._string(query, value, env, depth + 1)
                if text is not None:
                    values[param] = text
            body = _Query(function.name, function.steps, function.result, (), function.text)
            return _with_how(self._table(body, body.result, column, values, "", depth + 1),
                             f"returned by the function '{name}'")
        return _unresolved(f"{_where(step)}: {name} is not interpreted")

    def _derived(self, query, table_tokens, fn_tokens, column, env, step, depth, verb) -> list[Origin]:
        """A column computed from other columns of the same row (`each [A] * [B]`)."""
        fields = self._row_fields(query, fn_tokens)
        if not fields:
            return [Origin("generated", column=column, reason=f"{verb} in {_where(step)} from no column")]
        origins = []
        for name in fields:
            origins.extend(self._table(query, table_tokens, name, env, step, depth + 1))
        return _with_how(origins, f"{verb} in '{step}'")

    def _row_fields(self, query, tokens: list[_Tok]) -> list[str]:
        """The fields of the current row an `each` or a function reads: `[Qty]`, `_[Qty]`, `r[Qty]`."""
        fields: list[str] = []
        for i, token in enumerate(tokens):
            if token.kind == "ident" and token.text == "Record.Field" and i + 1 < len(tokens):
                call = _call(tokens[i:])
                if call and len(call[1]) >= 2:
                    value = _strip(call[1][1])
                    if len(value) == 1 and value[0].kind == "string":
                        fields.append(value[0].text)
            if not _is(token, "["):
                continue
            end = _closing(tokens, i)
            content = tokens[i + 1:end]
            if not content or any(t.kind == "punct" and t.text in ("=", ",") for t in content):
                continue                    # a record, not a field
            before = tokens[i - 1] if i else None
            if before is not None:
                if before.kind == "punct" and before.text in ("}", ")", "]"):
                    continue                # a field of another table's row
                if before.kind == "ident" and before.text != "_" and (
                        before.text in query.steps or self.query(before.text) is not None):
                    continue                # a field of another step or query
            if len(content) == 1:
                name = content[0].text
            else:                           # [Unit Price (EUR)]: the name as written
                name = query.text[content[0].start:content[-1].end].strip() if query.text else ""
            if name and name not in fields:
                fields.append(name)
        return fields

    def _joined(self, query, tokens, nested, env, depth) -> list[_Tok] | None:
        """The table merged as `nested` by the Table.NestedJoin before `tokens`."""
        for _ in range(_MAX_DEPTH):
            tokens = _strip(tokens)
            if len(tokens) == 1 and tokens[0].kind == "ident":
                if tokens[0].text not in query.steps:
                    return None
                tokens = query.steps[tokens[0].text]
                continue
            called = _call(tokens)
            if called is None or not called[1]:
                return None
            name, args, _rest = called
            if name == "Table.NestedJoin" and len(args) > 4 and \
                    self._string(query, args[4], env, depth) == nested:
                return args[2]
            tokens = args[0]
        return None

    # -- sources -------------------------------------------------------------------

    def _source(self, query, tokens, env, depth) -> _Source | None:
        if depth > _MAX_DEPTH:
            return None
        tokens = _strip(tokens)
        if len(tokens) == 1 and tokens[0].kind == "ident":
            name = tokens[0].text
            if name in query.steps:
                return self._source(query, query.steps[name], env, depth + 1)
            other = self.query(name)
            if other is not None and other is not query and not other.params:
                return self._source(other, other.result, {}, depth + 1)
            return None
        if len(tokens) > 1 and tokens[0].kind == "ident" and tokens[1].kind == "punct" \
                and tokens[1].text in ("{", "["):
            base = self._source(query, tokens[:1], env, depth + 1)
            return self._navigate_source(base, tokens[1:], query, env) if base else None
        called = _call(tokens)
        if called is None:
            return None
        name, args, rest = called
        if rest:
            base = self._source(query, tokens[:len(tokens) - len(rest)], env, depth + 1)
            return self._navigate_source(base, rest, query, env) if base else None
        strings = [self._string(query, a, env, depth + 1) for a in args]
        if name == "Sql.Database":
            options = _record(args[2]) if len(args) > 2 else None
            sql = self._string(query, options["Query"], env, depth + 1) if options and "Query" in options else None
            return _Source(name, strings[0] if strings else "", strings[1] if len(strings) > 1 else "",
                           query=sql)
        if name == "Sql.Databases":
            return _Source(name, strings[0] if strings else "")
        function = self.query(name)
        if function is not None and function.params:
            return None
        if "." in name and not name.startswith("Table."):
            literal = next((t.text for a in args for t in a if t.kind == "string"), "")
            return _Source(name, literal)
        return None

    def _navigate_source(self, source: _Source, postfix, query, env) -> _Source:
        """Navigation that doesn't reach a table yet (Sql.Databases -> database)."""
        result = _Source(source.connector, source.server, source.database, source.query, source.path)
        for record in self._accessors(postfix, query, env):
            if source.connector == "Sql.Databases" and not result.database and "Name" in record:
                result.database = record["Name"]
            elif record:
                result.path = result.path + (record.get("Item") or record.get("Name") or
                                             record.get("Id") or next(iter(record.values()), ""),)
        return result

    def _accessors(self, postfix, query, env) -> list[dict]:
        """The `{[Key=value]}` items of a navigation, as {key: string}; `[Data]` fields skipped."""
        records = []
        i = 0
        while i < len(postfix):
            token = postfix[i]
            if not (token.kind == "punct" and token.text in ("{", "[")):
                break
            end = _closing(postfix, i)
            if token.text == "{":
                inner = postfix[i + 1:end]
                record = _record(inner)
                if record is not None:
                    records.append({k: self._string(query, v, env, 0) or "" for k, v in record.items()})
                elif len(inner) == 1 and inner[0].kind == "number":
                    records.append({"Index": inner[0].text})
            i = end + 1
        return records

    def _navigate(self, source: _Source, postfix, column, query, env) -> Origin:
        records = self._accessors(postfix, query, env)
        database = source.database
        if source.connector in _SQL_SOURCES:
            for record in records:
                if not database and "Name" in record and "Schema" not in record:
                    database = record["Name"]
                    continue
                if "Item" in record or ("Name" in record and "Schema" in record):
                    return Origin("db", schema=record.get("Schema", ""),
                                  name=record.get("Item") or record.get("Name", ""), column=column,
                                  server=source.server, database=database, connector=source.connector)
            return Origin("unresolved", reason=f"{source.connector}: no table in the navigation")
        path = source.path + tuple(
            r.get("Item") or r.get("Name") or r.get("Id") or (f"#{r['Index']}" if "Index" in r else "")
            for r in records)
        named = [p for p in path if p]
        return Origin("external", name=named[-1] if named else "", column=column,
                      server=source.server, connector=source.connector)

    # -- values ------------------------------------------------------------------------

    def _string(self, query, tokens, env, depth) -> str | None:
        """The text value of an expression: a literal, a parameter, a step, a concatenation."""
        if depth > _MAX_DEPTH:
            return None
        tokens = _strip(tokens)
        meta = next((i for i, t in enumerate(tokens) if _word(t, "meta")), None)
        if meta is not None:
            tokens = tokens[:meta]
        parts = _split(tokens, "&")
        if len(parts) > 1:
            values = [self._string(query, p, env, depth + 1) for p in parts]
            return None if any(v is None for v in values) else "".join(values)
        if len(tokens) == 1:
            token = tokens[0]
            if token.kind == "string":
                return token.text
            if token.kind == "ident":
                if token.text in env:
                    return env[token.text]
                if token.text in query.steps:
                    return self._string(query, query.steps[token.text], env, depth + 1)
                other = self.query(token.text)
                if other is not None and other is not query and not other.params:
                    return self._string(other, other.result, {}, depth + 1)
        return None


def _rebuild(name: str, args: list[list[_Tok]]) -> list[_Tok]:
    """The tokens of `name(args...)`, for re-reading a call as a source."""
    tokens = [_Tok("ident", name, 0, 0), _Tok("punct", "(", 0, 0)]
    for i, arg in enumerate(args):
        if i:
            tokens.append(_Tok("punct", ",", 0, 0))
        tokens.extend(arg)
    tokens.append(_Tok("punct", ")", 0, 0))
    return tokens


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

_DIRECT_LAKE_RE = re.compile(r"^\s*(entityName|schemaName|expressionSource)\s*:\s*(.+?)\s*$")


def _unquote(value: str) -> str:
    text = tmdl.normalize_text_value(value)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1].replace(text[0] * 2, text[0])
    return text


def _partition_kind(obj) -> str:
    return (obj.block.inline_expression or "").strip().lower()


def _queries(definition: dict) -> dict[str, _Query]:
    """Every query of the model by name: shared expressions, then the tables' own queries."""
    queries: dict[str, _Query] = {}
    for name, obj in definition["expressions"].items():
        queries[name] = _parse_query(name, obj.expression())
    for table, entry in definition["tables"].items():
        for partition in entry["partitions"]:
            if _partition_kind(partition) in ("m", "policyrange", ""):
                text = tmdl.get_expression_property(partition.lines, partition.block, "source")
                if text and table not in queries:
                    queries[table] = _parse_query(table, text)
    return queries


def model_sources(project_path: str) -> list[TableSource]:
    """Every partition of the model with the origins of each of its columns."""
    definition = load_definition(project_path)
    tracer = _Tracer(_queries(definition))
    result: list[TableSource] = []
    for table, entry in sorted(definition["tables"].items()):
        data_columns = []
        for column in entry["column_objects"]:
            if column.block.inline_expression is not None:
                continue                            # calculated column: DAX
            source = tmdl.get_property(column.lines, column.block, "sourceColumn")
            data_columns.append((column.name, _unquote(source.value) if source and source.value
                                 else column.name))
        for partition in entry["partitions"]:
            kind = _partition_kind(partition)
            mode_property = tmdl.get_property(partition.lines, partition.block, "mode")
            mode = tmdl.normalize_text_value(mode_property.value) if mode_property else "import"
            item = TableSource(table, partition.name, "other", mode,
                               partition.file_path, partition.block.start + 1)
            if kind == "calculationgroup":
                item.kind = "calculation_group"         # its items are DAX, read with the measures
            elif kind == "calculated":
                item.kind = "calculated"
                item.dax = tmdl.get_expression_property(partition.lines, partition.block, "source") or ""
            elif kind == "entity":
                item.kind = "direct_lake"
                found = {}
                for line in partition.lines[partition.block.start + 1:partition.block.end]:
                    match = _DIRECT_LAKE_RE.match(line)
                    if match:
                        found[match.group(1)] = _unquote(match.group(2))
                for column, source in data_columns:
                    item.columns[column] = [Origin("db", schema=found.get("schemaName", "dbo"),
                                                   name=found.get("entityName", ""), column=source,
                                                   connector="Direct Lake",
                                                   database=found.get("expressionSource", ""))]
            elif kind in ("m", "policyrange", ""):
                item.kind = "m"
                text = tmdl.get_expression_property(partition.lines, partition.block, "source")
                query = _parse_query(table, text or "")
                for column, source in data_columns:
                    try:
                        item.columns[column] = tracer.column(query, source)
                    except Exception as exc:  # noqa: BLE001 - one column must not stop the model
                        item.columns[column] = _unresolved(
                            f"the Power Query of partition '{partition.name}' could not be read "
                            f"({type(exc).__name__})")
            result.append(item)
    return result
