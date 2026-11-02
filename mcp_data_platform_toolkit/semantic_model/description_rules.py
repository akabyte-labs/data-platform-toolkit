"""
The user's own rules for the Data Dictionary descriptions, added to the
built-in ones (`description_guidelines.md`, shipped in the package).

From the most general to the most specific:
  - the built-in rules, in the package: they improve with the server;
  - the user's rules for every project: 'description_guidelines.md' at the
    top of the user's folder (see `user_folder`);
  - the team's rules, only when someone chose to commit them:
    'description_guidelines.md' in the project folder or above it, up to
    the root of its Git repository;
  - the user's rules for one project: 'projects/<repository>/
    description_guidelines.md' in the user's folder.
Each level adds to the ones before it; a file that starts with
`<!-- replace -->` replaces them instead.

The first description ever written asks the user whether they have rules of
their own to add (see "The first-time question" below): once per user, not
per session or per project, and never when they already have some.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from mcp.server.elicitation import DeclinedElicitation
from mcp.server.mcpserver import Context, Elicit, ElicitationResult, Resolve
from pydantic import BaseModel, Field

from mcp_data_platform_toolkit.mcp_common import folder_choice, user_folder
from mcp_data_platform_toolkit.mcp_common.user_folder import ProjectIdentity
from mcp_data_platform_toolkit.semantic_model.project_paths import (
    check_output_path,
    find_semantic_model_folder,
)

RULES_FILE = user_folder.GENERAL_RULES_FILE       # the same name at every level
REPLACE_MARKER = "<!-- replace -->"
SCOPES = ("project", "general", "repo")
SAVE_MODES = ("append", "overwrite")
# A rules file is a few lines of conventions: anything this long is a mistake.
MAX_RULES_CHARS = 20_000

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


# ---------------------------------------------------------------------------
# The files of the rules
# ---------------------------------------------------------------------------

def project_rules(project_path: str) -> ProjectIdentity:
    """The Power BI project of `project_path`: the folder of its .SemanticModel, and its repository."""
    return user_folder.project_identity(Path(find_semantic_model_folder(project_path)).parent)


def general_file() -> Path:
    return user_folder.user_folder() / RULES_FILE


def project_file(project: ProjectIdentity) -> Path:
    return user_folder.project_dir(project) / RULES_FILE


def repo_file(project: ProjectIdentity) -> Path | None:
    """The team's rules: the closest 'description_guidelines.md' from the project folder up to the repository root."""
    current = project.folder
    top = project.repo_root or project.folder
    while True:
        candidate = current / RULES_FILE
        if candidate.is_file():
            return candidate
        if current == top or current.parent == current:
            return None
        current = current.parent


def repo_target(project: ProjectIdentity) -> Path:
    """Where the team's rules are written when asked: the repository root, or the project folder."""
    existing = repo_file(project)
    return existing or (project.repo_root or project.folder) / RULES_FILE


@dataclass(frozen=True)
class RuleSource:
    label: str
    path: Path | None
    exists: bool
    text: str               # the rules, comments removed
    replaces: bool = False  # starts with the replace marker


def _read_rules(label: str, path: Path | None) -> RuleSource:
    if path is None:
        return RuleSource(label, None, False, "")
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return RuleSource(label, path, False, "")
    except (OSError, UnicodeDecodeError) as exc:
        return RuleSource(label, path, True, f"(not readable: {exc.__class__.__name__})")
    first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    replaces = first.lower().replace(" ", "") == REPLACE_MARKER.replace(" ", "")
    clean: list[str] = []
    for line in _COMMENT_RE.sub("", raw).splitlines():
        line = line.rstrip()
        if not line and (not clean or not clean[-1]):
            continue                                # collapse blank lines
        clean.append(line)
    body = "\n".join(clean).strip()
    if body.startswith("# ") and "\n" not in body:
        body = ""                                   # only the template's title: no rules yet
    return RuleSource(label, path, True, body, replaces)


def rule_sources(project_path: str = "") -> list[RuleSource]:
    """
    The user's rule files that apply, from the most general; built-in
    excluded. The project's folder and rules file are created from the
    template when missing, so the user finds the file to edit.
    """
    sources = [_read_rules("yours", general_file())]
    if str(project_path or "").strip():
        project = project_rules(project_path)
        ensure_project_file(project)
        repo = repo_file(project)
        if repo is not None:
            sources.append(_read_rules("the repository's", repo))
        sources.append(_read_rules(f"project '{project.name}'", project_file(project)))
    return sources


def _describe(source: RuleSource) -> str:
    if not source.exists:
        state = "not created yet"
    elif not source.text:
        state = "no rules yet"
    else:
        state = "replaces the rules before it" if source.replaces else "in use"
    return f"{source.label}: {source.path} ({state})"


def rules_line(sources: list[RuleSource]) -> str:
    """One line with every file the rules come from, and how to change them."""
    parts = ["built-in"] + [_describe(source) for source in sources]
    return ("Rules in use: " + "; ".join(parts) + ". Edit those files to change the rules (they "
            "are read again at every call), or ask to add a rule (save_description_guidelines, "
            "full access mode).")


def compose(builtin: str, sources: list[RuleSource]) -> str:
    """The built-in rules followed by the user's, each under its file."""
    sections = [builtin]
    used = [source for source in sources if source.exists and source.text]
    for source in used:
        title = f"=== RULES: {source.label} ({source.path}) ==="
        if source.replaces:
            sections = []
            title += " - they replace the rules before them"
        sections.append(f"{title}\n{source.text}")
    if used:
        sections.append("Where these rules disagree, the more specific one wins: the project's "
                        "over the repository's, the repository's over yours, yours over the "
                        "built-in ones.")
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

GENERAL_TEMPLATE = """# My rules for the descriptions

<!--
Your conventions for the Data Dictionary descriptions, valid for every
project. They are ADDED to the built-in rules of Data Platform Toolkit:
write only what you want different or more specific. This file is read
again at every call, no restart is needed, and the text between these
comment marks is ignored.

Examples:
- Write the descriptions in British English.
- Call the revenue "turnover", never "income".
- Say the currency of every amount.
- Glossary of your company's terms, one per line: an acronym or an internal
  word is explained in a description only if it is defined here. For example:
  "OEE = overall equipment effectiveness".
-->
"""

PROJECT_TEMPLATE = """# Rules for the descriptions of {name}

<!--
Conventions valid only for the project '{name}'. They are added to the
built-in rules and to your own for every project (description_guidelines.md
at the top of your folder), and they win where they disagree. The text
between these comment marks is ignored.

Examples:
- The customers are called "clients"; the resellers "partners".
- The fiscal year starts in October.
- Glossary of this project, one term per line (what it means, and the rules of
  the business behind it): the assistant explains a term of the company only
  if it is defined here or by you. For example: "scrap paid = waste charged
  to the customer".
-->
"""

REPO_TEMPLATE = """# Rules for the descriptions of this repository

<!--
Shared with everyone who works on this repository, through Git. They are
added to the built-in rules of Data Platform Toolkit and to each person's
own; a person's rules for this project win where they disagree.
-->
"""


def ensure_general_file() -> Path:
    """The file of the rules for every project, created from its template when missing."""
    path = general_file()
    if not path.exists():
        user_folder.write_text(path, GENERAL_TEMPLATE)
    return path


def ensure_project_file(project: ProjectIdentity) -> Path:
    """The project's rules file, created with its folder from the template when missing (once the folder is chosen)."""
    path = user_folder.ensure_project_dir(project) / RULES_FILE
    if not path.exists() and path.parent.is_dir():
        try:
            user_folder.write_text(path, PROJECT_TEMPLATE.format(name=project.name))
        except OSError:
            pass
    return path


def settle_first_choice(choice=None) -> str:
    """
    The first-time choice of the user's folder (see `folder_choice`); once
    made, the folder starts with the template of the general rules, so the
    user finds a file to edit where the server said.
    """
    notice = folder_choice.settle_first_choice(choice)
    if notice:
        try:
            ensure_general_file()
        except OSError:
            pass
    return notice


def _validated_rules(rules: str) -> str:
    text = str(rules or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("'rules' is empty: give the text of the rules to save.")
    illegal = sorted({c for c in text if ord(c) < 32 and c not in "\n\t"})
    if illegal:
        raise ValueError("'rules' contains control characters that are not allowed ("
                         + ", ".join(f"U+{ord(c):04X}" for c in illegal) + ").")
    if len(text) > MAX_RULES_CHARS:
        raise ValueError(f"'rules' is {len(text)} characters: a rules file is meant for a few "
                         f"conventions (at most {MAX_RULES_CHARS}).")
    return text


def save_rules(rules: str, scope: str = "project", project_path: str = "",
               mode: str = "append") -> tuple[Path, bool]:
    """
    Adds `rules` to a rules file ('append') or makes them its whole content
    ('overwrite'), creating the file from its template. Returns (path,
    created). The repository's file is written only for scope 'repo', and
    only inside the project's folders.
    """
    if scope not in SCOPES:
        raise ValueError(f"'scope' must be one of: {', '.join(SCOPES)} (got '{scope}').")
    if mode not in SAVE_MODES:
        raise ValueError(f"'mode' must be one of: {', '.join(SAVE_MODES)} (got '{mode}').")
    text = _validated_rules(rules)
    if scope == "general":
        path, template = general_file(), GENERAL_TEMPLATE
    else:
        if not str(project_path or "").strip():
            raise ValueError(f"Give 'project_path' for the rules of scope '{scope}'.")
        project = project_rules(project_path)
        if scope == "project":
            path, template = project_file(project), PROJECT_TEMPLATE.format(name=project.name)
        else:
            path, template = repo_target(project), REPO_TEMPLATE
            folders = [str(project.repo_root or project.folder)]
            check_output_path(str(path), folders, "The rules file of the repository")
    created = not path.exists()
    if created:
        base = template                                 # a new file starts from its template
    elif mode == "append":
        base = path.read_text(encoding="utf-8-sig")
    else:
        base = ""                                       # 'overwrite': the rules are the whole file
    content = (base.rstrip("\n") + "\n\n" if base.strip() else "") + text + "\n"
    user_folder.write_text(path, content)
    return path, created


# ---------------------------------------------------------------------------
# The first-time question: does the user have rules of their own?
# ---------------------------------------------------------------------------

# In settings.json: the user was asked, whatever they answered.
GUIDELINES_ASKED = "guidelines_asked"


def needs_guidelines_question() -> bool:
    """
    True until the user is asked, once ever, whether they have rules of their
    own to add; never when their file for every project already has some.
    """
    if user_folder.setting(GUIDELINES_ASKED):
        return False
    return not any(source.exists and source.text for source in rule_sources())


class GuidelinesAnswer(BaseModel):
    """The form shown to the user, through the client, before the first description."""

    rules: str = Field(
        default="", title="Your rules",
        description="Your own rules for the descriptions, e.g. 'Write in British English; call the "
                    "revenue \"turnover\"'. Leave it empty if you have none.")


def guidelines_question_message() -> str:
    return (
        "Before the first description is written: do you have explicit rules for the "
        "descriptions to add to the built-in ones of Data Platform Toolkit (terminology, "
        "language, level of detail, phrases to use or avoid)? Write them below, or leave the "
        f"field empty. They are saved in your file for every project ({RULES_FILE} in your "
        "folder), which you can edit at any time. This question is asked only once."
    )


def ask_user_guidelines(ctx: Context):
    """Resolver of `GuidelinesQuestion`: the form, only until the user is asked and only to a client that can show it."""
    if not needs_guidelines_question() or not folder_choice.elicitation_supported(ctx):
        return None
    return Elicit(guidelines_question_message(), GuidelinesAnswer)


# A tool parameter of this type is filled by the user's answer, never by the
# language model: MCPServer leaves it out of the tool's input schema.
GuidelinesQuestion = Annotated[ElicitationResult[GuidelinesAnswer], Resolve(ask_user_guidelines)]


def settle_guidelines_question(answer=None, choice=None) -> tuple[str, str]:
    """
    Applies the outcome of the first-time question, before the first
    description is written. Returns (stop, notice): `stop` is the response of
    a write that must not happen yet ("" to go on), `notice` what to tell the
    user about their folder. The user counts as asked whatever the outcome,
    so the question never comes twice.
    """
    notice = settle_first_choice(choice)
    if not needs_guidelines_question():
        return "", notice
    data = getattr(answer, "data", None)
    if isinstance(data, GuidelinesAnswer):
        user_folder.remember(GUIDELINES_ASKED, True)
        if not data.rules.strip():
            return "", notice
        path, _created = save_rules(data.rules, "general")
        return (
            f"Nothing was written. The user gave rules of their own, now saved in '{path}': "
            f"call get_description_guidelines, revise the description with them, and call "
            f"this tool again." + (f"\n{notice}" if notice else ""), "")
    user_folder.remember(GUIDELINES_ASKED, True)
    if isinstance(answer, DeclinedElicitation):
        return "", notice           # the user chose not to answer: the built-in rules apply
    # No form (the client can't show one): the assistant asks instead.
    return (
        "Nothing was written. This is the first description written with Data Platform "
        "Toolkit: before writing it, ask the user whether they have explicit rules for the "
        "descriptions to add to the built-in ones (terminology, language, level of detail, "
        "phrases to use or avoid), and wait for the answer. If they give some, save them "
        "with save_description_guidelines (scope 'general' unless they want them for this "
        "project only), call get_description_guidelines and revise the description with "
        "them. Then call this tool again. The question is not asked again."
        + (f"\n{notice}" if notice else ""), "")
