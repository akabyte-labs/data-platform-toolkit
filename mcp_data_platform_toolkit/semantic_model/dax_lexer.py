"""
Minimal DAX tokenizer, used to find the references of an expression.

It replaces a single regular expression that got several common shapes of
DAX wrong, each one reproduced on real expressions:
  - `RETURN [Total]` and `NOT [Flag]` read as the column `RETURN[Total]`,
    losing the dependency on the measure;
  - `-- [Old]` comments (DAX has three comment forms, not two) counted as
    references;
  - unquoted table names with non-ASCII letters (`Café[Amount]`) losing
    the table;
  - `]]` inside a bracketed name ending the name early.

The tokenizer knows nothing about the model: it says what the expression
references, and `dax_lineage` decides what each reference is (measure,
column, calculation group) against the loaded model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Words that can precede `[...]` without being a table: `RETURN [X]` is an
# unqualified reference, not the column X of a table named RETURN. A model
# table with one of these names (e.g. 'Order') is still recognized, because
# the final decision is taken against the model's table names.
DAX_KEYWORDS = {
    "RETURN", "VAR", "NOT", "IN", "DEFINE", "EVALUATE", "ORDER", "BY", "ASC",
    "DESC", "START", "AT", "MEASURE", "COLUMN", "TABLE", "FUNCTION", "AND", "OR",
}

# Enumeration constants written as bare words (DATEADD(..., MONTH),
# CROSSFILTER(..., BOTH), RANKX(..., DENSE), CONVERT(..., INTEGER)). Bare,
# they are read as constants, not as tables: most are also function names,
# and a table named like a DAX function must be quoted anyway ('Month'); a
# quoted table name is never affected. Used only to keep them out of the
# iteration context, not to decide what precedes `[...]`.
DAX_CONSTANTS = {
    "DAY", "WEEK", "MONTH", "QUARTER", "YEAR", "HOUR", "MINUTE", "SECOND",
    "TRUE", "FALSE", "BOTH", "NONE", "ONEWAY", "ONEWAY_LEFTFILTERS", "ONEWAY_RIGHTFILTERS",
    "SKIP", "DENSE", "INTEGER", "DOUBLE", "STRING", "BOOLEAN", "CURRENCY", "DATETIME",
    "REL", "ABS",
}

# Functions through which a calculation item applies to the measure in context.
SELECTED_MEASURE_FUNCTIONS = {"SELECTEDMEASURE", "SELECTEDMEASURENAME", "ISSELECTEDMEASURE",
                              "SELECTEDMEASUREFORMATSTRING"}


@dataclass
class Token:
    kind: str       # string | quoted | bracket | ident | number | op
    value: str


@dataclass
class DaxReference:
    table: str              # "" if unqualified
    name: str               # the text between brackets
    table_quoted: bool = False

    @property
    def qualified(self) -> bool:
        return bool(self.table)


@dataclass
class DaxAnalysis:
    references: list[DaxReference] = field(default_factory=list)
    # Bare table names (`SUMX(Sales, ...)`, `COUNTROWS('Sales')`): they give
    # the iteration context needed to resolve unqualified columns.
    tables: list[str] = field(default_factory=list)
    strings: list[str] = field(default_factory=list)
    functions: set[str] = field(default_factory=set)
    variables: set[str] = field(default_factory=set)


def _read_delimited(text: str, start: int, close: str) -> tuple[str, int]:
    """Reads up to `close`, where a doubled `close` is an escape. Returns (value, next index)."""
    chars: list[str] = []
    index = start
    while index < len(text):
        char = text[index]
        if char == close:
            if index + 1 < len(text) and text[index + 1] == close:
                chars.append(close)
                index += 2
                continue
            return "".join(chars), index + 1
        chars.append(char)
        index += 1
    return "".join(chars), index      # unterminated: take the rest


def _is_ident_start(char: str) -> bool:
    return char.isalpha() or char == "_"


def tokenize(expression: str) -> list[Token]:
    """Tokens of a DAX expression, without whitespace and comments."""
    text = expression or ""
    tokens: list[Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
        elif text.startswith(("//", "--"), index):
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
        elif char == '"':
            value, index = _read_delimited(text, index + 1, '"')
            tokens.append(Token("string", value))
        elif char == "'":
            value, index = _read_delimited(text, index + 1, "'")
            tokens.append(Token("quoted", value))
        elif char == "[":
            value, index = _read_delimited(text, index + 1, "]")
            tokens.append(Token("bracket", value))
        elif _is_ident_start(char):
            end = index + 1
            # The dot belongs to names such as PERCENTILE.INC or T.DIST.
            while end < length and (text[end].isalnum() or text[end] == "_"
                                    or (text[end] == "." and end + 1 < length
                                        and text[end + 1].isalnum())):
                end += 1
            tokens.append(Token("ident", text[index:end]))
            index = end
        elif char.isdigit():
            end = index + 1
            while end < length and (text[end].isalnum() or text[end] == "."):
                end += 1
            tokens.append(Token("number", text[index:end]))
            index = end
        else:
            tokens.append(Token("op", char))
            index += 1
    return tokens


def analyze(expression: str) -> DaxAnalysis:
    """References, bare tables, string literals, functions and variables of an expression."""
    tokens = tokenize(expression)
    result = DaxAnalysis()
    seen: set[tuple[str, str]] = set()

    for position, token in enumerate(tokens):
        previous = tokens[position - 1] if position > 0 else None
        following = tokens[position + 1] if position + 1 < len(tokens) else None

        if token.kind == "bracket":
            name = token.value.strip()
            if not name:
                continue
            # `Sales[OrderDate].[Year]`: a level of the date column's auto
            # date/time variation, not a reference of its own.
            if (previous is not None and previous.kind == "op" and previous.value == "."
                    and position > 1 and tokens[position - 2].kind == "bracket"):
                continue
            # An unquoted word before the brackets may be a keyword
            # (`RETURN [X]`) or a table: it is kept as written and decided
            # by the caller, which can check it against the model's tables.
            table, quoted = "", False
            if previous is not None and previous.kind == "quoted":
                table, quoted = previous.value.strip(), True
            elif previous is not None and previous.kind == "ident":
                table = previous.value
            key = (table.lower(), name.lower())
            if key not in seen:
                seen.add(key)
                result.references.append(DaxReference(table, name, quoted))

        elif token.kind == "string":
            result.strings.append(token.value)

        elif token.kind == "quoted":
            if following is None or following.kind != "bracket":
                result.tables.append(token.value.strip())

        elif token.kind == "ident":
            upper = token.value.upper()
            if following is not None and following.kind == "op" and following.value == "(":
                result.functions.add(upper)
            elif following is not None and following.kind == "bracket":
                continue                      # table prefix, already handled
            elif previous is not None and previous.kind == "ident" and previous.value.upper() == "VAR":
                result.variables.add(token.value.lower())
            elif upper not in DAX_KEYWORDS and upper not in DAX_CONSTANTS:
                result.tables.append(token.value)

    result.tables = [t for t in dict.fromkeys(result.tables) if t.lower() not in result.variables]
    return result


def is_keyword(word: str) -> bool:
    return (word or "").upper() in DAX_KEYWORDS


def extract_references(expression: str) -> list[tuple[str, str]]:
    """
    [(table_or_empty_string, element)] without knowledge of the model: a
    keyword before the brackets makes the reference unqualified.
    """
    references: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in analyze(expression).references:
        table = "" if (not ref.table_quoted and is_keyword(ref.table)) else ref.table
        key = (table.lower(), ref.name.lower())
        if key not in seen:
            seen.add(key)
            references.append((table, ref.name))
    return references
