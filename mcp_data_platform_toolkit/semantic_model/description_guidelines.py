"""
Guidelines for the descriptions meant for a business Data Dictionary.

They are split into two levels so as not to waste tokens:
  - `SHORT_GUIDELINES`: a few lines, always appended to the description of
    the tools that accept a `base_description`, so the constraint is visible
    without calling anything;
  - the full version with examples, returned on request by the
    `get_description_guidelines` tool once at the start of a documentation
    session: the built-in rules (`description_guidelines.md`, shipped in the
    package) followed by the user's own (see `description_rules`).

The built-in rules live in a text file and not in the code because they are
editorial content. They are not edited in place any more: an installed
package is replaced at every update. Whoever wants other conventions writes
them in their own rules files, which add to these.
"""

from __future__ import annotations

from pathlib import Path

from mcp_data_platform_toolkit.mcp_common.errors import ToolError
from mcp_data_platform_toolkit.mcp_common.output_shaping import MAX_TOOL_OUTPUT_CHARS, shape_lines
from mcp_data_platform_toolkit.semantic_model import description_rules

# Shipped inside the package, next to this module: the path is computed from
# the file, not from the folder the server was launched from.
GUIDELINES_FILE = Path(__file__).resolve().parent / "description_guidelines.md"

# Single definition of the short text: the tools' docstrings don't repeat it,
# they receive it from `with_short_guidelines()`.
SHORT_GUIDELINES = (
    "GUIDELINES for base_description (Data Dictionary, business readers):\n"
    "1-3 sentences, max ~50 words, plain language. Explain what the data represents\n"
    "and, if useful, the logic in natural language (period, filters, exclusions).\n"
    "Forbidden: DAX syntax/functions, Table[Column] notation, data modeling\n"
    "terms, internal codes, markdown. Say only what the DAX, the context, the\n"
    "existing description or the user support: no invented agreements,\n"
    "thresholds or causes, keep the facts of the existing text; if unclear, ask.\n"
    "Before documenting several elements call\n"
    "'get_description_guidelines' once, with the project_path, for the full\n"
    "guidelines with examples and the user's own rules for the project."
)


def load_full_guidelines() -> str:
    """
    Full text of the built-in guidelines, normalized (no trailing spaces, no
    consecutive blank lines).
    """
    try:
        raw = GUIDELINES_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(
            f"Guidelines not available: cannot read '{GUIDELINES_FILE}' ({exc.__class__.__name__}). "
            f"The file is part of the package: reinstall it."
        ) from exc

    lines: list[str] = []
    for line in raw.splitlines():
        clean = line.rstrip()
        if not clean and (not lines or not lines[-1]):
            continue  # collapse consecutive blank lines
        lines.append(clean)

    text = "\n".join(lines).strip()
    if not text:
        raise ToolError(f"Empty guidelines: the file '{GUIDELINES_FILE}' contains no text.")
    return text


def guidelines_for(project_path: str = "", notice: str = "") -> str:
    """
    The rules that apply, as `get_description_guidelines` returns them: the
    line naming every file they come from (and `notice`, the outcome of the
    first-time choice of the folder), the built-in rules, then the user's.
    The user's files are read again at every call: they are edited while
    the server runs.
    """
    sources = description_rules.rule_sources(project_path)
    header = description_rules.rules_line(sources) + (f"\n{notice}" if notice else "")
    text = f"{header}\n\n{description_rules.compose(load_full_guidelines(), sources)}"
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return shape_lines(text, MAX_TOOL_OUTPUT_CHARS, note="shorten your rules files")


def with_short_guidelines(doc: str | None) -> str:
    """
    Description of a tool with the short version of the guidelines appended
    once. Used by `get_smart_description_preview` (reader) and by
    `write_smart_description` (writer), which live in different modules.
    """
    text = (doc or "").strip()
    if SHORT_GUIDELINES in text:
        return text
    return f"{text}\n\n{SHORT_GUIDELINES}" if text else SHORT_GUIDELINES
