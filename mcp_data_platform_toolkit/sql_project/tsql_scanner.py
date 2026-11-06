"""
What has to happen to T-SQL text before the parser can read it.

sqlglot reads a single statement well, but a database project is made of
batches and modules it doesn't fully understand:

  - batches separated by `GO`, and SQLCMD lines (`:r`, `:setvar`);
  - `OPENROWSET(BULK ...)`, which fails to parse and took the whole file
    with it: it is replaced by a placeholder table, keeping the path read
    and the columns of its `WITH (...)` clause;
  - procedures: with TRY/CATCH, IF or dynamic SQL the parser falls back to
    opaque text and the statements inside are lost. The data statements
    (INSERT, MERGE, UPDATE, SELECT INTO, DELETE) are cut out of the body
    here and parsed one by one; control flow carries no lineage.

Dynamic SQL (`EXEC(@sql)`, `sp_executesql`) can't be analyzed: it is
reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlglot.dialects.tsql import TSQL
from sqlglot.errors import TokenError
from sqlglot.tokens import TokenType

FILE_SCHEMA = "__files__"      # schema of the OPENROWSET placeholders

_GO_RE = re.compile(r"^[ \t]*GO(?:[ \t]+\d+)?[ \t]*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)
_SQLCMD_RE = re.compile(r"^[ \t]*:[A-Za-z].*$", re.MULTILINE)
_OPENROWSET_RE = re.compile(r"\bOPENROWSET\s*\(", re.IGNORECASE)
_BULK_PATH_RE = re.compile(r"\bBULK\s+N?'((?:[^']|'')*)'", re.IGNORECASE)
_WITH_RE = re.compile(r"\s*WITH\s*\(", re.IGNORECASE)
# One part of a (possibly qualified) name: [bracketed], "quoted" or plain.
# A plain part can't contain [, ] or ", so each part is read in only one way:
# otherwise `[a.a]` is both one bracketed part and `[a` . `a]`, and a long
# name that doesn't match backtracks exponentially (ReDoS).
_NAME_PART = r"(?:\[[^\]]+\]|\"[^\"]+\"|[^\s.(\[\]\"]+)"
_QUALIFIED_NAME = rf"{_NAME_PART}(?:\s*\.\s*{_NAME_PART})*"
_MODULE_RE = re.compile(
    r"^\s*(?:CREATE\s+OR\s+ALTER|CREATE|ALTER)\s+(PROC(?:EDURE)?|FUNCTION|TRIGGER)\s+"
    rf"({_QUALIFIED_NAME})",
    re.IGNORECASE)
_CREATE_TABLE_RE = re.compile(
    rf"^\s*CREATE\s+TABLE\s+({_QUALIFIED_NAME})\s*\(",
    re.IGNORECASE)
# Items of a table definition that are not columns.
_TABLE_ITEMS = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "INDEX", "PERIOD"}
_MULTI_STATEMENT_FUNCTION_RE = re.compile(r"\bRETURNS\s+@", re.IGNORECASE)
_INLINE_FUNCTION_RE = re.compile(r"\bRETURNS\s+TABLE\b", re.IGNORECASE)


@dataclass
class Batch:
    text: str
    line: int                       # line of the text's first character (offset 0) in the file


@dataclass
class FileRead:
    """An OPENROWSET(BULK ...) replaced by the placeholder table FILE_SCHEMA.name."""
    name: str
    path: str
    columns: list[str] | None       # from WITH (...), None when there is none


@dataclass
class Segment:
    """A data statement cut out of a module body."""
    text: str
    offset: int                     # in the batch text
    kind: str                       # insert, merge, update, delete, select, with


@dataclass
class ModuleBody:
    segments: list[Segment] = field(default_factory=list)
    dynamic_sql: list[int] = field(default_factory=list)       # offsets
    insert_exec: list[int] = field(default_factory=list)       # offsets


def split_batches(text: str) -> list[Batch]:
    """Batches separated by `GO`, with SQLCMD lines blanked (line numbers stay right)."""
    clean = _SQLCMD_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    batches, start, line = [], 0, 1
    for match in _GO_RE.finditer(clean):
        chunk = clean[start:match.start()]
        if chunk.strip():
            batches.append(Batch(chunk, line))
        line += clean[start:match.end()].count("\n")
        start = match.end()
    chunk = clean[start:]
    if chunk.strip():
        batches.append(Batch(chunk, line))
    return batches


def line_of(batch: Batch, offset: int) -> int:
    return batch.line + batch.text.count("\n", 0, max(offset, 0))


def first_line(batch: Batch) -> int:
    """The line of the batch's first token (after blank lines and comments)."""
    return line_of(batch, len(batch.text) - len(_strip_leading_comments(batch.text)))


# ---------------------------------------------------------------------------
# OPENROWSET
# ---------------------------------------------------------------------------

def _closing_paren(text: str, open_index: int) -> int:
    """Index after the ')' matching text[open_index] == '(' (strings and brackets skipped)."""
    depth, i = 0, open_index
    while i < len(text):
        char = text[i]
        if char == "'":
            i = text.find("'", i + 1)
            while i != -1 and text[i + 1:i + 2] == "'":
                i = text.find("'", i + 2)
            if i == -1:
                break
        elif char == "[":
            i = text.find("]", i + 1)
            if i == -1:
                break
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _column_names(definitions: str) -> list[str]:
    """Names of a column list 'A int, [B] varchar(10) COLLATE x, C decimal(10, 2)'."""
    names, depth, current = [], 0, ""
    for char in definitions + ",":
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            part = current.strip()
            if part:
                first = re.match(r"\[([^\]]+)\]|\"([^\"]+)\"|(\S+)", part)
                if first:
                    names.append(next(g for g in first.groups() if g))
            current = ""
            continue
        current += char
    return names


def rewrite_openrowset(sql: str, counter: list[int]) -> tuple[str, list[FileRead]]:
    """
    Replaces every OPENROWSET(BULK ...) [WITH (...)] with a placeholder table
    and returns what it read. The replacement keeps the newlines of the text it
    replaces, so line numbers don't move. `counter` numbers the placeholders
    across a file.
    """
    reads: list[FileRead] = []
    out, position = [], 0
    for match in _OPENROWSET_RE.finditer(sql):
        if match.start() < position:
            continue
        end = _closing_paren(sql, match.end() - 1)
        if end == -1:
            break
        inner = sql[match.end():end - 1]
        bulk = _BULK_PATH_RE.search(inner)
        if bulk is None:
            continue                        # OPENROWSET over a provider, not files: left as is
        columns = None
        with_clause = _WITH_RE.match(sql, end)
        if with_clause is not None:
            with_end = _closing_paren(sql, with_clause.end() - 1)
            if with_end != -1:
                columns = _column_names(sql[with_clause.end():with_end - 1])
                end = with_end
        name = f"f{counter[0]}"
        counter[0] += 1
        reads.append(FileRead(name=name, path=bulk.group(1).replace("''", "'"), columns=columns))
        replaced = sql[match.start():end]
        out.append(sql[position:match.start()])
        out.append(f"[{FILE_SCHEMA}].[{name}]" + "\n" * replaced.count("\n"))
        position = end
    out.append(sql[position:])
    return "".join(out), reads


# ---------------------------------------------------------------------------
# OUTPUT clauses
# ---------------------------------------------------------------------------

# Where an OUTPUT clause ends: the next clause of its statement.
_AFTER_OUTPUT = {TokenType.SELECT, TokenType.VALUES, TokenType.DEFAULT, TokenType.FROM,
                 TokenType.WHERE, TokenType.EXECUTE, TokenType.SEMICOLON, TokenType.WITH}


def strip_output_clauses(sql: str) -> str:
    """
    The statement without its `OUTPUT ... [INTO target (columns)]` clauses,
    which the parser can't read in MERGE and INSERT. They copy the written
    rows to an audit table or a variable: not the data the lineage follows.
    Newlines are kept, so line numbers don't move.
    """
    if "output" not in sql.lower():
        return sql
    try:
        tokens = TSQL().tokenize(sql)
    except TokenError:
        return sql
    cuts, depth, start = [], 0, None
    for token in tokens:
        if token.token_type == TokenType.L_PAREN:
            depth += 1
        elif token.token_type == TokenType.R_PAREN:
            depth -= 1
        if depth != 0:
            continue
        if start is None and token.token_type == TokenType.RETURNING and token.text.upper() == "OUTPUT":
            start = token.start
        elif start is not None and token.token_type in _AFTER_OUTPUT:
            cuts.append((start, token.start))
            start = None
    if start is not None:
        cuts.append((start, len(sql)))
    for begin, end in reversed(cuts):
        sql = sql[:begin] + "\n" * sql.count("\n", begin, end) + " " + sql[end:]
    return sql


def table_definition(batch_text: str) -> tuple[str, list[str]] | None:
    """
    (dotted name, column names) of a CREATE TABLE, read without the parser:
    for the tables it can't read (storage clauses inside a constraint, system
    versioning, memory-optimized options), which would otherwise be lost.
    None if the batch is not one, or its column list doesn't close.
    """
    text = _strip_leading_comments(batch_text.lstrip("\ufeff"))
    match = _CREATE_TABLE_RE.match(text)
    if match is None:
        return None
    end = _closing_paren(text, match.end() - 1)
    if end == -1:
        return None
    names = []
    for name in _column_names(text[match.end():end - 1]):
        if name.upper() not in _TABLE_ITEMS:
            names.append(name)
    return match.group(1), names


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------

def module_header(batch_text: str) -> tuple[str, str] | None:
    """('procedure' | 'function' | 'trigger', dotted name) of a CREATE/ALTER module."""
    match = _MODULE_RE.match(_strip_leading_comments(batch_text))
    if match is None:
        return None
    kind = match.group(1).lower()
    kind = "procedure" if kind.startswith("proc") else kind
    return kind, match.group(2)


def function_kind(batch_text: str) -> str:
    """'inline' (RETURNS TABLE), 'multi_statement' (RETURNS @t TABLE) or 'scalar'."""
    if _MULTI_STATEMENT_FUNCTION_RE.search(batch_text):
        return "multi_statement"
    return "inline" if _INLINE_FUNCTION_RE.search(batch_text) else "scalar"


def _strip_leading_comments(text: str) -> str:
    stripped = text
    while True:
        stripped = stripped.lstrip()
        if stripped.startswith("--"):
            newline = stripped.find("\n")
            stripped = "" if newline == -1 else stripped[newline + 1:]
        elif stripped.startswith("/*"):
            end = stripped.find("*/")
            stripped = "" if end == -1 else stripped[end + 2:]
        else:
            return stripped


_STATEMENT_STARTS = {
    TokenType.INSERT: "insert", TokenType.MERGE: "merge", TokenType.UPDATE: "update",
    TokenType.DELETE: "delete", TokenType.SELECT: "select", TokenType.WITH: "with",
}
# Tokens that end the statement in progress (T-SQL doesn't require ';').
_STOP_TYPES = {
    TokenType.BEGIN, TokenType.ELSE, TokenType.DECLARE, TokenType.EXECUTE, TokenType.TRUNCATE,
    TokenType.COMMIT, TokenType.ROLLBACK, TokenType.FETCH, TokenType.COMMAND, TokenType.SET,
}
_STOP_WORDS = {"IF", "WHILE", "RETURN", "THROW", "RAISERROR", "BREAK", "CONTINUE", "GOTO",
               "OPEN", "CLOSE", "DEALLOCATE", "PRINT", "WAITFOR"}
_PROCEDURE_OPTIONS = {"RECOMPILE", "ENCRYPTION", "EXECUTE", "EXEC", "NATIVE_COMPILATION",
                      "SCHEMABINDING", "RETURNS"}
_SET_OPERATORS = {TokenType.UNION, TokenType.EXCEPT, TokenType.INTERSECT, TokenType.ALL,
                  TokenType.DISTINCT}


def _word(token) -> str:
    return token.text.upper() if token.token_type == TokenType.VAR else ""


def _body_start(tokens) -> int:
    """
    Index of the first token of a procedure body: the one after the `AS` that
    closes the header. An `AS` right after a parameter name (`@p AS int`) is
    a parameter type, not the end of the header.
    """
    depth = 0
    for i, token in enumerate(tokens):
        if token.token_type == TokenType.L_PAREN:
            depth += 1
        elif token.token_type == TokenType.R_PAREN:
            depth -= 1
        elif token.token_type == TokenType.ALIAS and depth == 0:
            if i >= 2 and tokens[i - 2].token_type == TokenType.PARAMETER:
                continue
            return i + 1
    return len(tokens)


def scan_module_body(batch_text: str, skip_header: bool = True) -> ModuleBody:
    """
    The data statements of a procedure body (or of any batch the parser
    couldn't read), each cut where the next statement or control-flow keyword
    starts, at parenthesis and CASE depth zero.
    """
    try:
        tokens = TSQL().tokenize(batch_text)
    except TokenError:
        return ModuleBody()
    body = ModuleBody()
    start_index = _body_start(tokens) if skip_header else 0

    current: dict | None = None             # {kind, start, end, select, set, values}

    def close():
        nonlocal current
        if current is not None and current["kind"] != "select_plain":
            text = batch_text[current["start"]:current["end"]]
            body.segments.append(Segment(text=text, offset=current["start"], kind=current["kind"]))
        current = None

    depth = case_depth = 0
    for i in range(start_index, len(tokens)):
        token = tokens[i]
        kind = token.token_type
        prev = tokens[i - 1] if i > start_index else None
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None

        if kind == TokenType.L_PAREN:
            depth += 1
        elif kind == TokenType.R_PAREN:
            depth -= 1
        elif kind == TokenType.CASE:
            case_depth += 1
        elif kind == TokenType.END and case_depth > 0:
            case_depth -= 1
            if current is not None:
                current["end"] = token.end + 1
            continue

        if depth == 0 and case_depth == 0:
            if kind == TokenType.SEMICOLON:
                close()
                continue
            if kind == TokenType.EXECUTE:
                following = nxt.text.lower() if nxt is not None else ""
                if following == "(" or following in ("sp_executesql", "dbo.sp_executesql"):
                    body.dynamic_sql.append(token.start)
                elif following == "as":
                    continue                # EXECUTE AS: an option, not a call
                if current is not None and current["kind"] == "insert" and not current["select"]:
                    body.insert_exec.append(current["start"])
                    current = None
                    continue
            if kind == TokenType.WITH and nxt is not None and nxt.text.upper() in _PROCEDURE_OPTIONS:
                continue
            if kind == TokenType.WITH and nxt is not None and nxt.token_type == TokenType.L_PAREN:
                pass                        # table hint: FROM t WITH (NOLOCK), MERGE t WITH (HOLDLOCK)
            elif _continues(current, token, prev):
                pass
            elif kind in _STATEMENT_STARTS:
                close()
                statement = _STATEMENT_STARTS[kind]
                current = {"kind": statement, "start": token.start, "end": token.end + 1,
                           "select": False, "set": False, "values": False}
                if statement == "select":
                    current["kind"] = "select_plain"
            elif kind in _STOP_TYPES or _word(token) in _STOP_WORDS or (
                    kind == TokenType.END and case_depth == 0):
                close()
                continue

        if current is not None:
            current["end"] = token.end + 1
            if depth == 0 and case_depth == 0:
                if kind == TokenType.INTO and current["kind"] == "select_plain":
                    current["kind"] = "select"          # SELECT ... INTO: a write
                elif kind == TokenType.VALUES:
                    current["values"] = True
    close()
    return body


def _continues(current: dict | None, token, prev) -> bool:
    """Whether a statement keyword at depth zero belongs to the statement in progress."""
    if current is None:
        return False
    kind = token.token_type
    statement = current["kind"]
    if kind == TokenType.SELECT:
        if prev is not None and prev.token_type in _SET_OPERATORS:
            return True                     # ... UNION [ALL] SELECT
        if statement in ("insert", "with") and not current["select"] and not current["values"]:
            current["select"] = True
            if statement == "with":
                current["kind"] = "select_plain"
            return True
        return False
    if kind in (TokenType.INSERT, TokenType.UPDATE, TokenType.DELETE, TokenType.MERGE):
        if prev is not None and prev.token_type == TokenType.THEN:
            return True                     # MERGE ... WHEN MATCHED THEN UPDATE / INSERT / DELETE
        if statement == "with" and not current["select"]:
            current["kind"] = _STATEMENT_STARTS[kind]
            return True                     # WITH cte AS (...) INSERT / UPDATE / MERGE / DELETE
        return False
    if kind == TokenType.SET:
        if prev is not None and prev.token_type == TokenType.UPDATE:
            return True                     # MERGE ... THEN UPDATE SET
        if statement == "update" and not current["set"]:
            current["set"] = True
            return True
        return False
    return False
