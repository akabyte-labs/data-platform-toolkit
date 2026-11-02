"""
Minimal, shared TMDL parser.

Replaces the ad-hoc implementations based on `content.split("measure ")` and
substring comparisons that used to be scattered across utils.py and
reader_tools.py. Exposes a block-based model (`TmdlBlock`) reused by reading,
DAX lineage, automatic documentation and writing.

TMDL syntax implemented (ref. Microsoft Learn, "TMDL overview"):
  - object declaration : <keyword> <name> [= <default property>]
  - property           : <name>: <value>       -> ':' delimiter, NOT '='
  - boolean property    : <name>                -> implicit true
                          <name>: true|false    -> key/value form
  - object names        : wrapped in single quotes if they contain . = : '
                          or spaces; an internal quote is doubled ('')
  - text values          : double quotes optional, required if the value has
                          leading/trailing spaces; an internal double quote
                          is doubled ("")
  - multi-line expressions: indented one level deeper than properties; may be
                          wrapped in ```

All functions operate on `lines` (a list of lines with trailing newline, as
returned by `file.readlines()`) so that surgical edits can preserve the rest
of the file.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass


class TmdlError(ValueError):
    """
    TMDL parsing or writing error: content or input that can't be read or
    written as valid TMDL. A ValueError, so the tool boundary treats it as an
    expected failure (one log line) and not as a defect (stack trace).
    """


class ElementNotFoundError(TmdlError):
    """The declaration of the requested element does not exist in the file."""


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

OBJECT_KEYWORDS = {
    "database", "model", "table", "column", "measure", "partition", "hierarchy",
    "level", "calculationGroup", "calculationItem", "relationship", "role",
    "tablePermission", "columnPermission", "perspective", "culture", "expression",
    "queryGroup", "annotation", "extendedProperty", "changedProperty", "variation",
    "refreshPolicy", "dataSource", "namedExpression", "linguisticMetadata",
    # DAX user-defined functions ('functions.tmdl'), the KPI of a measure and
    # the members of a perspective: without them a 'function' was invisible
    # and a bare 'kpi' line was read as a boolean property.
    "function", "kpi", "perspectiveTable", "perspectiveColumn", "perspectiveMeasure",
    "perspectiveHierarchy",
    # A culture as Power BI Desktop writes it ('cultureInfo en-US') and the
    # section holding its translations: without them the culture was no
    # block at all and a bare 'translations' line was read as a boolean.
    "cultureInfo", "translations",
}

_KEYWORD_BY_LOWER = {k.lower(): k for k in OBJECT_KEYWORDS}

# Aliases accepted by the tools, mapped to the real TMDL keyword.
ELEMENT_TYPE_ALIASES = {
    "table": "table",
    "measure": "measure",
    "column": "column",
    "dimension": "column",
    "calculationgroup": "calculationGroup",
    "calcitem": "calculationItem",
    "calculationitem": "calculationItem",
    "hierarchy": "hierarchy",
    "partition": "partition",
    "role": "role",
    "tablepermission": "tablePermission",
    "columnpermission": "columnPermission",
}

# Known boolean properties: accept both the shorthand and the key/value form.
BOOLEAN_PROPERTIES = {
    "ishidden", "iskey", "isavailableinmdx", "isdefaultlabel", "isdefaultimage",
    "isactive", "isnullable", "isunique", "isprivate", "isdatacategory",
    "ismeasure", "rediscoveringdatasources",
}

_FENCE = "```"

_DECLARATION_RE = re.compile(r"^(?P<kw>[A-Za-z][A-Za-z0-9_]*)(?:\s+(?P<rest>.*))?$")
_PROPERTY_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<value>.*)$")
_BOOL_SHORTCUT_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)$")
# Property whose value is a multi-line expression: `source =`, `formatStringDefinition =`.
_EXPRESSION_PROPERTY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=\s*$")
# Any expression property, whatever follows the '='.
_EXPRESSION_PROPERTY_LINE_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=")

# Values of `fenced_mask`.
LINE_NORMAL = 0
LINE_FENCE = 1          # a ``` delimiter line
LINE_FENCED = 2         # content between two delimiters


def _opens_fence(stripped: str) -> bool:
    """
    A declaration or expression property whose expression starts with a
    fence ON THE SAME LINE, right after the '=' (`measure X = ````,
    `source = ````), the form Power BI Desktop writes. The closing delimiter
    is the next line that starts with ```.

    Only those two line shapes: any line ending with ``` and containing '='
    used to qualify, so a line of an unfenced DAX or M body such as
    `VAR x = 1 // see ```` opened a fence, and every object after it
    disappeared exactly as with a misread closing delimiter.
    """
    if not stripped.endswith(_FENCE):
        return False
    match = _DECLARATION_RE.match(stripped)
    if match and match.group("kw").lower() in _KEYWORD_BY_LOWER:
        return _split_name_and_expression((match.group("rest") or "").strip())[1] == _FENCE
    match = _EXPRESSION_PROPERTY_LINE_RE.match(stripped)
    return match is not None and stripped[match.end():].strip() == _FENCE


def fenced_mask(lines: list[str], start: int = 0, end: int | None = None) -> list[int]:
    """
    Classifies the lines of `lines[start:end]` as normal, fence delimiter or
    fenced content; `mask[i]` describes `lines[start + i]`.

    The previous parsers toggled a flag on every line STARTING with ```. With
    the opening fence on the declaration line (`measure X = ````) the closing
    delimiter was taken for an opening one: the rest of the file became
    "fenced" and every object after that measure silently disappeared from
    lineage, reads and writes.

    Starting at a block's declaration is always correct (a declaration is
    never inside a fence), and it keeps per-block callers from rescanning the
    whole file: on a 3,000-measure model that made the lineage 25x slower.
    """
    end = len(lines) if end is None else end
    mask = [LINE_NORMAL] * (end - start)
    fenced = False
    for offset, line in enumerate(lines[start:end]):
        index = offset
        stripped = line.strip()
        if fenced:
            if stripped.startswith(_FENCE):
                mask[index] = LINE_FENCE
                fenced = False
            else:
                mask[index] = LINE_FENCED
            continue
        if stripped.startswith(_FENCE):
            mask[index] = LINE_FENCE
            fenced = True
        elif _opens_fence(stripped):
            fenced = True
    return mask


def normalize_element_type(element_type: str) -> str:
    """Translates an element-type alias into the corresponding TMDL keyword."""
    keyword = ELEMENT_TYPE_ALIASES.get(str(element_type or "").strip().lower())
    if not keyword:
        allowed = ", ".join(sorted(set(ELEMENT_TYPE_ALIASES)))
        raise TmdlError(f"Invalid element type: '{element_type}'. Allowed: {allowed}.")
    return keyword


# ---------------------------------------------------------------------------
# Indentation
# ---------------------------------------------------------------------------

def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def detect_indent_unit(lines: list[str]) -> str:
    """Infers the file's indentation unit (tab or N spaces)."""
    for line in lines:
        if line.startswith("\t"):
            return "\t"
    for line in lines:
        if not line.strip():
            continue
        indent = indent_of(line)
        if indent:
            return indent
    return "\t"


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------

def _split_name_and_expression(rest: str) -> tuple[str, str | None]:
    """From 'name = expression' to (name, expression). Handles quoted names."""
    if rest.startswith("'"):
        chars: list[str] = []
        i = 1
        while i < len(rest):
            char = rest[i]
            if char == "'":
                if i + 1 < len(rest) and rest[i + 1] == "'":
                    chars.append("'")
                    i += 2
                    continue
                i += 1
                break
            chars.append(char)
            i += 1
        name = "".join(chars)
        tail = rest[i:].strip()
    elif "=" in rest:
        raw_name, raw_tail = rest.split("=", 1)
        name = raw_name.strip()
        tail = "=" + raw_tail
    else:
        return rest.strip(), None

    if tail.startswith("="):
        return name, tail[1:].strip()
    return name, None


def parse_declaration(line: str) -> tuple[str, str, str | None] | None:
    """
    If the line is a TMDL object declaration, returns
    (keyword, name, inline_expression). `inline_expression` is None if the
    object has no default property, "" if the expression is multi-line.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("///") or stripped.startswith(_FENCE):
        return None
    match = _DECLARATION_RE.match(stripped)
    if not match:
        return None
    keyword = _KEYWORD_BY_LOWER.get(match.group("kw").lower())
    if not keyword:
        return None
    rest = (match.group("rest") or "").strip()
    if not rest:
        return keyword, "", None
    name, expression = _split_name_and_expression(rest)
    if expression == _FENCE:
        # `measure X = ````: multi-line fenced expression, not the text '```'.
        expression = ""
    return keyword, name, expression


def parse_property_line(line: str) -> tuple[str, str | None] | None:
    """
    If the line is a property, returns (name, raw_value).
    The value is None in the boolean shorthand form (`isHidden`).
    """
    stripped = line.strip()
    match = _PROPERTY_RE.match(stripped)
    if match:
        return match.group("name"), match.group("value").strip()
    match = _BOOL_SHORTCUT_RE.match(stripped)
    if match and match.group("name").lower() not in _KEYWORD_BY_LOWER:
        return match.group("name"), None
    return None


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

@dataclass
class TmdlBlock:
    keyword: str
    name: str
    start: int                      # index of the declaration line
    end: int                        # exclusive end-of-block index
    indent: str
    inline_expression: str | None = None
    doc_start: int = -1             # start of the preceding '///' block

    @property
    def declaration_line_index(self) -> int:
        return self.start


@dataclass
class TmdlChild:
    index: int
    kind: str                       # property | object | doc | other
    name: str | None = None
    value: str | None = None
    keyword: str | None = None


def find_block_end(lines: list[str], start: int) -> int:
    """
    End of the block starting at `start`, exclusive. Based only on
    indentation: properties and expressions are always deeper than the
    declaration, fenced content is part of the block whatever its indent.
    Trailing blank lines are not included. Same fence rules as `fenced_mask`,
    tracked incrementally because the end is not known in advance.
    """
    base = len(indent_of(lines[start]))
    last_content = start
    fenced = _opens_fence(lines[start].strip())
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if fenced:
            if stripped.startswith(_FENCE):
                fenced = False
            last_content = index
            continue
        if stripped.startswith(_FENCE):
            fenced = True
            last_content = index
            continue
        if not stripped:
            continue
        if len(indent_of(lines[index])) <= base:
            break
        if _opens_fence(stripped):
            fenced = True
        last_content = index
    return last_content + 1


def make_block(lines: list[str], start: int) -> TmdlBlock:
    declaration = parse_declaration(lines[start])
    if declaration is None:
        raise TmdlError(f"Line {start + 1} is not a valid TMDL declaration.")
    keyword, name, expression = declaration

    # The '///' lines right above the declaration are its description, but
    # only if they are not deeper than it: a DAX line starting with '///' at
    # the end of the PREVIOUS object's expression used to become the
    # description of this one (and be removed or rewritten with it).
    doc_start = start
    depth = len(indent_of(lines[start]))
    index = start - 1
    while (index >= 0 and lines[index].strip().startswith("///")
           and len(indent_of(lines[index])) <= depth):
        doc_start = index
        index -= 1

    return TmdlBlock(
        keyword=keyword,
        name=name,
        start=start,
        end=find_block_end(lines, start),
        indent=indent_of(lines[start]),
        inline_expression=expression,
        doc_start=doc_start,
    )


def find_all_blocks(lines: list[str]) -> list[TmdlBlock]:
    """
    All blocks declared in the file, at any nesting level.

    The body of a multi-line expression is skipped: a DAX line such as
    `column` or an M line such as `expression Source = ...` inside a
    partition used to be read as a declaration, creating phantom objects.
    TMDL indents an object's expression deeper than its properties, and a
    property's expression (`source =`) deeper than the property itself.
    """
    mask = fenced_mask(lines)
    unit = len(detect_indent_unit(lines))
    blocks: list[TmdlBlock] = []
    skip_deeper_than: int | None = None
    for index, line in enumerate(lines):
        if mask[index] != LINE_NORMAL:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("///"):
            continue
        depth = len(indent_of(line))
        if skip_deeper_than is not None:
            if depth > skip_deeper_than:
                continue
            skip_deeper_than = None
        declaration = parse_declaration(line)
        if declaration is not None:
            blocks.append(make_block(lines, index))
            # Any default expression, also one that starts on the declaration
            # line (`measure A = CALCULATE(`) and continues on deeper lines:
            # those lines are expression, never declarations.
            if declaration[2] is not None and not _opens_fence(stripped):
                skip_deeper_than = depth + unit
        elif _EXPRESSION_PROPERTY_RE.match(stripped):
            skip_deeper_than = depth
    return blocks


def find_block(lines: list[str], element_name: str, element_type: str) -> TmdlBlock:
    keyword = normalize_element_type(element_type)
    target = str(element_name or "").strip().lower()
    for block in find_all_blocks(lines):
        if block.keyword == keyword and block.name.strip().lower() == target:
            return block
    raise ElementNotFoundError(
        f"No declaration found for element '{element_name}' of type '{element_type}'."
    )


def find_block_any_type(lines: list[str], element_name: str, element_types: list[str]) -> TmdlBlock:
    """Looks up the element trying several types in order (used for the calculationItem fallback)."""
    errors = []
    for element_type in element_types:
        try:
            return find_block(lines, element_name, element_type)
        except ElementNotFoundError as exc:
            errors.append(str(exc))
    raise ElementNotFoundError(
        f"Element '{element_name}' not found with any of the types {element_types}."
    )


def find_table_block(lines: list[str]) -> TmdlBlock:
    for block in find_all_blocks(lines):
        if block.keyword == "table":
            return block
    raise ElementNotFoundError("No 'table' declaration found in the TMDL file.")


def child_indent(lines: list[str], block: TmdlBlock) -> str:
    """
    Indentation of the direct children (property level). If the block only
    contains multi-line expression lines (deeper), falls back to
    `block_indent + unit`.
    """
    unit = detect_indent_unit(lines)
    candidate = block.indent + unit
    shallowest: str | None = None
    mask = fenced_mask(lines, block.start, block.end)
    for index in range(block.start + 1, block.end):
        line = lines[index]
        if mask[index - block.start] != LINE_NORMAL or not line.strip():
            continue
        indent = indent_of(line)
        if len(indent) <= len(block.indent):
            continue
        if shallowest is None or len(indent) < len(shallowest):
            shallowest = indent
    if shallowest is None or len(shallowest) > len(candidate):
        return candidate
    return shallowest


def iter_children(lines: list[str], block: TmdlBlock) -> list[TmdlChild]:
    """Direct children of the block (properties, nested objects, comments)."""
    target_len = len(child_indent(lines, block))
    children: list[TmdlChild] = []
    mask = fenced_mask(lines, block.start, block.end)
    for index in range(block.start + 1, block.end):
        line = lines[index]
        stripped = line.strip()
        if mask[index - block.start] != LINE_NORMAL or not stripped:
            continue
        if len(indent_of(line)) != target_len:
            continue
        if stripped.startswith("///"):
            children.append(TmdlChild(index=index, kind="doc"))
            continue
        prop = parse_property_line(line)
        if prop is not None:
            children.append(TmdlChild(index=index, kind="property", name=prop[0], value=prop[1]))
            continue
        declaration = parse_declaration(line)
        if declaration is not None:
            children.append(
                TmdlChild(index=index, kind="object", name=declaration[1], keyword=declaration[0])
            )
            continue
        children.append(TmdlChild(index=index, kind="other"))
    return children


def child_blocks(lines: list[str], block: TmdlBlock, keyword: str | None = None) -> list[TmdlBlock]:
    result = []
    for child in iter_children(lines, block):
        if child.kind != "object":
            continue
        if keyword and child.keyword != keyword:
            continue
        result.append(make_block(lines, child.index))
    return result


def get_expression(lines: list[str], block: TmdlBlock) -> str:
    """
    The block's DAX/M expression (inline or multi-line), without properties.

    An expression that starts on the declaration line can continue on the
    deeper lines below it (`measure A = CALCULATE(` + arguments): returning
    only the first line lost every reference after it.
    """
    if block.inline_expression is None:
        return ""
    if block.inline_expression and block.end <= block.start + 1:
        return block.inline_expression
    limit = len(child_indent(lines, block))
    collected: list[str] = []
    mask = fenced_mask(lines, block.start, block.end)
    for index in range(block.start + 1, block.end):
        line = lines[index]
        if mask[index - block.start] == LINE_FENCE:
            continue
        if mask[index - block.start] == LINE_FENCED:
            collected.append(line.rstrip("\n"))
            continue
        if not line.strip():
            if collected:
                collected.append("")
            continue
        if len(indent_of(line)) <= limit:
            break
        collected.append(line.rstrip("\n"))
    # Only blank lines are trimmed at the edges: `.strip()` also removed the
    # first line's own indentation, flattening `CALCULATE(` + its indented
    # arguments into a misaligned body.
    body = textwrap.dedent("\n".join(collected)).strip("\n").rstrip()
    if block.inline_expression:
        return f"{block.inline_expression}\n{body}" if body else block.inline_expression
    return body


def _expression_body_end(lines: list[str], block: TmdlBlock) -> int:
    """
    Exclusive index of the last line of the block's default expression,
    continuation lines of an inline one included; `block.start + 1` if it
    has none. Same rules as `get_expression`: fenced lines and lines deeper
    than the properties.
    """
    body_end = block.start + 1
    if block.inline_expression is None:
        return body_end
    limit = len(child_indent(lines, block))
    mask = fenced_mask(lines, block.start, block.end)
    for index in range(block.start + 1, block.end):
        line = lines[index]
        if mask[index - block.start] != LINE_NORMAL:
            body_end = index + 1
            continue
        if not line.strip():
            continue
        if len(indent_of(line)) <= limit:
            break
        body_end = index + 1
    return body_end


def set_default_value(lines: list[str], block: TmdlBlock, value: str | None) -> list[str]:
    """
    Rewrites the block's default property as a single-line value
    (`keyword Name = value`), or removes it when `value` is None. A previous
    multi-line or fenced body is dropped; properties and child objects are
    kept as they are. Meant for short scalar defaults (e.g. the
    metadataPermission of a 'columnPermission'), not for DAX/M expressions.
    """
    header = f"{block.indent}{block.keyword} {quote_object_name(block.name)}"
    if value is not None:
        text = _reject_control_characters(str(value).strip(), "The default value")
        if not text:
            raise TmdlError("The default value cannot be empty.")
        header += f" = {text}"
    updated = list(lines)
    updated[block.start:_expression_body_end(lines, block)] = [header + "\n"]
    return updated


def get_doc_comments(lines: list[str], block: TmdlBlock) -> list[str]:
    """'///' lines preceding the declaration, without the trailing newline."""
    if block.doc_start >= block.start:
        return []
    return [line.rstrip("\n") for line in lines[block.doc_start:block.start]]


# ---------------------------------------------------------------------------
# Property values
# ---------------------------------------------------------------------------

def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise TmdlError(f"Invalid boolean value: '{value}'. Use true/false.")


def _reject_control_characters(text: str, description: str) -> str:
    """
    TMDL is a line-based format with NO escape for newlines: a value that
    contains one is not badly quoted, it is written across two lines, and the
    parser reads the second one back as a property of its own. It is the
    vector through which a `displayFolder` such as 'Folder\nisHidden: true'
    injected a property into the model, and through which a name with a line
    break split a declaration in two. Quoting cannot protect against this:
    the only correct answer is to reject the value.
    """
    if "\n" in text or "\r" in text:
        raise TmdlError(
            f"{description} contains a line break. TMDL has no escape for "
            f"newlines: the value would be written across several lines and the file "
            f"could no longer be read by this server or by Power BI Desktop."
        )
    _reject_illegal_characters(text, description, allowed="\t")
    return text


def _reject_illegal_characters(text: str, description: str, allowed: str) -> None:
    illegal = sorted({c for c in text if ord(c) < 32 and c not in allowed})
    if illegal:
        codes = ", ".join(f"U+{ord(c):04X}" for c in illegal)
        raise TmdlError(
            f"{description} contains control characters that are not allowed ({codes})."
        )


def _reject_fence(text: str, description: str) -> None:
    """
    Three backticks open a verbatim (fenced) expression in TMDL, and there is
    no escape for them. Written inside an expression or a property value they
    made the parser read the rest of the file as part of that text: every
    column, partition and annotation after it disappeared, while the tool
    reported success.
    """
    if _FENCE in text:
        raise TmdlError(
            f"{description} contains '{_FENCE}'. In TMDL three backticks delimit a "
            f"verbatim expression and cannot be escaped, so they are refused in everything "
            f"this server writes: inside an expression or a value they would hide every "
            f"object after this one from Power BI Desktop and from this server."
        )


def normalize_multiline_text(text, description: str) -> list[str]:
    """
    Lines of a multi-line text written into TMDL (DAX expression, '///'
    description), validated.

    Line breaks are the only control characters with a meaning there: they
    become separate lines. Any other control character (NUL, BEL, vertical
    tab...) used to be written into the file as is. The split is on '\\n'
    only, the same rule the readers apply, so a character such as U+2028
    inside a DAX string is kept as text instead of becoming a line break.

    Three backticks are refused in descriptions too: a '///' line can't open
    a fence for this parser, but nothing guarantees every TMDL reader agrees,
    and one rule for everything written is simpler to trust.
    """
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    _reject_illegal_characters(normalized, description, allowed="\t\n")
    _reject_fence(normalized, description)
    return normalized.split("\n")


def format_text_value(value) -> str:
    text = "" if value is None else str(value)
    _reject_control_characters(text, "The property value")
    # `displayFolder: x = ```` would open a fenced expression.
    _reject_fence(text, "The property value")
    if text == "" or text != text.strip() or '"' in text:
        return '"' + text.replace('"', '""') + '"'
    return text


def format_property_value(property_name: str, value) -> str:
    if property_name.lower() in BOOLEAN_PROPERTIES or isinstance(value, bool):
        return "true" if _coerce_bool(value) else "false"
    return format_text_value(value)


def normalize_text_value(raw: str | None) -> str:
    if raw is None:
        return ""
    text = raw.strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return text[1:-1].replace('""', '"')
    return text


def quote_object_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise TmdlError("The element name cannot be empty.")
    _reject_control_characters(text, f"The name '{text[:60]}'")
    if any(char in text for char in ".=:'") or any(char.isspace() for char in text):
        return "'" + text.replace("'", "''") + "'"
    return text


def get_property(lines: list[str], block: TmdlBlock, property_name: str) -> TmdlChild | None:
    for child in iter_children(lines, block):
        if child.kind == "property" and (child.name or "").lower() == property_name.lower():
            return child
    return None


def get_bool_property(lines: list[str], block: TmdlBlock, property_name: str) -> bool:
    child = get_property(lines, block, property_name)
    if child is None:
        return False
    if child.value is None:
        return True
    return normalize_text_value(child.value).lower() == "true"


def is_hidden(lines: list[str], block: TmdlBlock) -> bool:
    """
    True only if the block itself declares isHidden among its own direct
    properties. Does not inherit isHidden from its children (a bug in the
    previous version, which made "hidden" any table with at least one hidden
    column).
    """
    return get_bool_property(lines, block, "isHidden")


def insert_lines(lines: list[str], index: int, new_lines: list[str]) -> list[str]:
    """
    Inserts lines, making sure the previous one ends with '\n'.

    A .tmdl file whose last character is not a newline (several editors
    produce them, and Git doesn't complain) made the new line end up AT THE
    END of the previous one: 'formatString: 0' + 'displayFolder: X' became a
    single line, the model no longer opened in Power BI Desktop, and the tool
    still reported success because the write itself had succeeded.
    `create_tmdl_element` and `append_child_block` already had this defense:
    it was missing on the property path, which is the most frequently used.
    """
    updated = list(lines)
    if 0 < index <= len(updated) and updated[index - 1] and not updated[index - 1].endswith("\n"):
        updated[index - 1] += "\n"
    updated[index:index] = new_lines
    return updated


def _property_insert_index(lines: list[str], block: TmdlBlock) -> int:
    """Properties go after the expression and before child objects."""
    last_property: TmdlChild | None = None
    for child in iter_children(lines, block):
        if child.kind == "object":
            return child.index if last_property is None else last_property.index + 1
        if child.kind == "property":
            last_property = child
    if last_property is not None:
        return last_property.index + 1
    return block.end


def property_line(indent: str, property_name: str, value) -> str:
    """
    A property line as Power BI Desktop writes it: `name: value`, and a true
    boolean in the shorthand form (`isHidden`). 'isHidden: true' is valid
    TMDL, but Desktop rewrites it as 'isHidden' on its next save: a diff on
    every element touched here.
    """
    formatted = format_property_value(property_name, value)
    if property_name.lower() in BOOLEAN_PROPERTIES and formatted == "true":
        return f"{indent}{property_name}\n"
    return f"{indent}{property_name}: {formatted}\n"


def set_property(lines: list[str], block: TmdlBlock, property_name: str, value) -> tuple[list[str], bool]:
    """
    Sets (or updates) a block property.
    Returns (new_lines, changed).
    """
    formatted = format_property_value(property_name, value)
    target_value = normalize_text_value(formatted)
    existing = get_property(lines, block, property_name)
    new_line = property_line(child_indent(lines, block), property_name, value)

    if existing is not None:
        current = "true" if existing.value is None else normalize_text_value(existing.value)
        if property_name.lower() in BOOLEAN_PROPERTIES:
            if current.lower() == target_value.lower():
                return lines, False
        elif current == target_value:
            return lines, False
        updated = list(lines)
        updated[existing.index] = new_line
        return updated, True

    return insert_lines(lines, _property_insert_index(lines, block), [new_line]), True


def remove_property(lines: list[str], block: TmdlBlock, property_name: str) -> tuple[list[str], bool]:
    """Removes a block property, if present. Returns (new_lines, removed)."""
    existing = get_property(lines, block, property_name)
    if existing is None:
        return lines, False
    updated = list(lines)
    del updated[existing.index]
    return updated, True


# ---------------------------------------------------------------------------
# Expression properties (`formatStringDefinition = ...`)
# ---------------------------------------------------------------------------
# Some properties hold an expression and use '=' instead of ':', like a
# default property but on their own line under the object. They are not
# `name: value` properties, so `get_property` / `set_property` don't see them.

def _expression_property_span(lines: list[str], block: TmdlBlock, property_name: str) -> tuple[int, int] | None:
    """(first, exclusive end) of the lines of an expression property, body included."""
    target_len = len(child_indent(lines, block))
    mask = fenced_mask(lines, block.start, block.end)
    for index in range(block.start + 1, block.end):
        line = lines[index]
        if mask[index - block.start] != LINE_NORMAL or not line.strip():
            continue
        if len(indent_of(line)) != target_len:
            continue
        match = _EXPRESSION_PROPERTY_LINE_RE.match(line.strip())
        if not match or match.group("name").lower() != property_name.lower():
            continue
        end = index + 1
        for following in range(index + 1, block.end):
            if mask[following - block.start] != LINE_NORMAL:
                end = following + 1
                continue
            text = lines[following]
            if not text.strip():
                continue
            if len(indent_of(text)) <= target_len:
                break
            end = following + 1
        return index, end
    return None


def get_expression_property(lines: list[str], block: TmdlBlock, property_name: str) -> str | None:
    """
    The value of an expression property (`source = ...` of a partition), on
    the same line or on the deeper lines below, fenced or not; None if the
    block doesn't have it.
    """
    span = _expression_property_span(lines, block, property_name)
    if span is None:
        return None
    first, end = span
    head = lines[first].strip().split("=", 1)[1].strip()
    if head == _FENCE:
        head = ""
    mask = fenced_mask(lines, block.start, block.end)
    body = [lines[i].rstrip("\n") for i in range(first + 1, end)
            if mask[i - block.start] != LINE_FENCE]
    text = textwrap.dedent("\n".join(body)).strip("\n").rstrip()
    return f"{head}\n{text}".strip() if head else text


def set_expression_property(lines: list[str], block: TmdlBlock, property_name: str,
                            expression: str) -> tuple[list[str], bool]:
    """
    Sets a single-line expression property (`formatStringDefinition = "0.0%"`),
    replacing the previous one whatever its form (single-line, multi-line or
    fenced). Returns (new_lines, changed).
    """
    parts = normalize_multiline_text(expression, f"The {property_name} expression")
    value = parts[0].strip() if len(parts) == 1 else ""
    if not value:
        raise TmdlError(f"The {property_name} expression must be a single non-empty line.")
    new_line = f"{child_indent(lines, block)}{property_name} = {value}\n"

    span = _expression_property_span(lines, block, property_name)
    if span is not None:
        start, end = span
        if end == start + 1 and lines[start].rstrip("\r\n") == new_line.rstrip("\n"):
            return lines, False
        updated = list(lines)
        updated[start:end] = [new_line]
        return updated, True
    return insert_lines(lines, _property_insert_index(lines, block), [new_line]), True


# ---------------------------------------------------------------------------
# Removing / inserting generic child blocks
# ---------------------------------------------------------------------------

def remove_block(lines: list[str], block: TmdlBlock) -> list[str]:
    """
    Removes a whole block (including the '///' lines that precede it).
    If removing it leaves two consecutive blank lines, one is collapsed.
    """
    start = block.doc_start
    end = block.end
    updated = lines[:start] + lines[end:]
    if 0 < start < len(updated) and not updated[start - 1].strip() and not updated[start].strip():
        del updated[start]
    return updated


def append_child_block(lines: list[str], parent: TmdlBlock, new_block_lines: list[str]) -> list[str]:
    """
    Appends `new_block_lines` as the last direct child of `parent`, before the
    end of the block. Inserts a separating blank line if the previous child
    doesn't already have one.
    """
    insert_at = parent.end
    block_lines = list(new_block_lines)
    if insert_at > 0 and lines[insert_at - 1].strip():
        block_lines = ["\n"] + block_lines
    # `insert_lines` normalizes the terminator of the previous line even when
    # it is blank but lacks '\n', a case the `.strip()` check alone let through.
    return insert_lines(lines, insert_at, block_lines)


def is_block_empty(lines: list[str], block: TmdlBlock) -> bool:
    """True if the block has neither a default expression nor properties/children."""
    if get_expression(lines, block).strip():
        return False
    return not any(child.kind in ("property", "object") for child in iter_children(lines, block))


def build_simple_element_lines(
    keyword: str,
    name: str,
    indent: str,
    unit: str,
    properties: list[tuple[str, object]] | None = None,
    doc_lines: list[str] | None = None,
) -> list[str]:
    """
    TMDL lines for a new element WITHOUT a default property (e.g. a
    'tablePermission' used only for OLS, a 'columnPermission'). Unlike
    `build_element_lines` it does not require a DAX/M expression.
    """
    output = build_doc_lines(doc_lines, indent)

    output.append(f"{indent}{keyword} {quote_object_name(name)}\n")
    for property_name, property_value in properties or []:
        if property_value is None:
            continue
        output.append(property_line(f"{indent}{unit}", property_name, property_value))
    return output


# ---------------------------------------------------------------------------
# Building new elements
# ---------------------------------------------------------------------------

def build_doc_lines(doc_lines: list[str] | None, indent: str) -> list[str]:
    """'///' lines of a description, validated: one per non-empty line of text."""
    output: list[str] = []
    for doc in doc_lines or []:
        for text in normalize_multiline_text(doc, "The description"):
            if text.strip():
                output.append(f"{indent}/// {text.strip()}\n")
    return output


def build_element_lines(
    keyword: str,
    name: str,
    expression: str,
    indent: str,
    unit: str,
    properties: list[tuple[str, object]] | None = None,
    doc_lines: list[str] | None = None,
    expression_properties: list[tuple[str, str]] | None = None,
) -> list[str]:
    """
    TMDL lines for a new element with a default property (measure /
    calculationItem). The multi-line expression is indented one level deeper
    than the properties, as required by the spec. `expression_properties`
    are written as `name = expression` (e.g. `formatStringDefinition`).
    """
    output = build_doc_lines(doc_lines, indent)

    quoted = quote_object_name(name)
    expression_lines = normalize_multiline_text(expression, "The DAX expression")
    # Only blank lines are trimmed at the edges, as when reading: `.strip()`
    # also removed the first line's own indentation, and an expression whose
    # first line was indented under the others came back misaligned.
    normalized = textwrap.dedent("\n".join(expression_lines)).strip("\n").rstrip()
    if not normalized.strip():
        raise TmdlError("The DAX expression cannot be empty.")
    expression_lines = normalized.split("\n")

    if len(expression_lines) == 1:
        output.append(f"{indent}{keyword} {quoted} = {expression_lines[0].strip()}\n")
    else:
        output.append(f"{indent}{keyword} {quoted} =\n")
        for line in expression_lines:
            output.append(f"{indent}{unit}{unit}{line.rstrip()}\n" if line.strip() else "\n")

    for property_name, property_value in properties or []:
        if property_value is None:
            continue
        output.append(property_line(f"{indent}{unit}", property_name, property_value))

    for property_name, property_expression in expression_properties or []:
        parts = normalize_multiline_text(property_expression, f"The {property_name} expression")
        if len(parts) != 1 or not parts[0].strip():
            raise TmdlError(f"The {property_name} expression must be a single non-empty line.")
        output.append(f"{indent}{unit}{property_name} = {parts[0].strip()}\n")

    return output


def find_insertion_index(lines: list[str], table_block: TmdlBlock, keyword: str) -> int:
    """
    Insertion point for a new child of the table:
      1. after the last sibling of the same type;
      2. otherwise before the first 'partition' / 'annotation' block;
      3. otherwise at the end of the table block.
    Avoids the previous version's behavior of appending to the end of the
    file, past the annotations and outside the table block.
    """
    last_same: TmdlBlock | None = None
    first_tail: TmdlChild | None = None
    for child in iter_children(lines, table_block):
        if child.kind != "object":
            continue
        if child.keyword == keyword:
            last_same = make_block(lines, child.index)
        elif child.keyword in ("partition", "annotation") and first_tail is None:
            first_tail = child
    if last_same is not None:
        return last_same.end
    if first_tail is not None:
        return first_tail.index
    return table_block.end
