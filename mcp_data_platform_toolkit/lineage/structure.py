"""
The structure of a database project that has no lineage configuration.

Without a 'lineage.yaml' (the repository's or the user's own) the standard
medallion applies: schemas named bronze, silver and gold. A warehouse that
names its schemas otherwise (stg, etl, dwh, pbi...) has no object in any
layer, and a lineage that says almost nothing. When the lineage tools meet
such a project they do not guess in silence: they infer the structure from
the SQL, as draft_lineage_config does, show it to the user and ask whether to
keep it or change it. Only when the user says yes it is saved, as their own
configuration, in their folder, outside the repository.

The question is a form shown by the client (MCP elicitation): the answer
reaches the server without going through the language model, which is why a
read-only server may save the file in the user's folder after it. A client
that can't show forms gets the structure through the assistant instead, once,
and the lineage then uses it as a provisional structure, marked as such in
every response, until the user saves it.

Nothing here is imported at start-up that needs the SQL parser: the lineage
reports a missing package when it is used, as the other lineage tools do.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server.elicitation import CancelledElicitation, DeclinedElicitation
from mcp.server.mcpserver import Context, Elicit, ElicitationResult, Resolve
from pydantic import BaseModel, Field

from mcp_data_platform_toolkit.mcp_common import user_folder
from mcp_data_platform_toolkit.mcp_common.folder_choice import elicitation_supported, settle_first_choice

if TYPE_CHECKING:
    from mcp_data_platform_toolkit.lineage.config_draft import Draft

# Below this share of its objects in a layer, a configuration describes nothing.
COVERED_SHARE = 0.5

# Rules, open points and schemas shown in the question: the rest is counted.
_SHOWN_RULES = 8
_SHOWN_POINTS = 6


class StructureAnswer(BaseModel):
    """The form shown to the user, through the client, when a project has no structure."""

    decision: Literal["save", "change", "not now"] = Field(
        default="save", title="Decision",
        description="save = keep this structure as yours; change = say below what to change; "
                    "not now = use it only for this session, without saving it.")
    changes: str = Field(
        default="", title="What to change",
        description="Only for 'change': what to change, in your words (for example: "
                    "'dwh is gold, not silver; ignore cfg').")


# ---------------------------------------------------------------------------
# Does the project need a structure?
# ---------------------------------------------------------------------------

def _paths(sql_project_path: str):
    from mcp_data_platform_toolkit.sql_project.config import personal_config_path
    from mcp_data_platform_toolkit.sql_project.sql_files import find_database_project
    project = find_database_project(sql_project_path)
    return project, personal_config_path(project.folder, project.sqlproj)


def structure_state(sql_project_path: str, config_path: str = "") -> str:
    """
    'none' when the project has no configuration, 'blank' when its own file
    says nothing, and in both cases only if the standard layers place less
    than half of its objects; '' otherwise. The repository's file is the
    team's: it is never replaced by a proposal.
    """
    if config_path or not str(sql_project_path or "").strip():
        return ""
    from mcp_data_platform_toolkit.sql_project.config import (DEFAULT_SCHEMA, LineageConfig, find_config_file,
                                                              load_config)
    from mcp_data_platform_toolkit.sql_project.project import (layer_coverage, load_unconfigured_lineage,
                                                               with_layers)
    from mcp_data_platform_toolkit.sql_project.sql_files import project_default_schema
    project, personal = _paths(sql_project_path)
    if find_config_file(project.folder):
        return ""
    state = "none"
    if os.path.isfile(personal):
        try:
            if not load_config(personal).blank:
                return ""
        except (ValueError, OSError):                   # a file with problems is for test_lineage_config
            return ""
        state = "blank"
    default_schema = project_default_schema(project.sqlproj) or DEFAULT_SCHEMA
    base = load_unconfigured_lineage(sql_project_path, default_schema)
    placed, total = layer_coverage(with_layers(base, LineageConfig(path="", default_schema=default_schema)))
    return state if total and placed < COVERED_SHARE * total else ""


# ---------------------------------------------------------------------------
# The proposal
# ---------------------------------------------------------------------------

_proposals: dict[tuple, tuple] = {}


def propose(sql_project_path: str, project_path: str = "") -> "Draft":
    """The draft of the project's structure at its proposed answers (kept while its SQL doesn't change)."""
    from mcp_data_platform_toolkit.lineage.config_draft import draft_config
    from mcp_data_platform_toolkit.semantic_model.tmdl_io import file_fingerprint
    from mcp_data_platform_toolkit.sql_project.sql_files import discover_sql_files, find_database_project
    project = find_database_project(sql_project_path)
    files, _excluded = discover_sql_files(project)
    stamp = tuple((path, file_fingerprint(path)) for path in [f.path for f in files] + [project.sqlproj])
    key = (os.path.normcase(project.sqlproj), (project_path or "").strip())
    hit = _proposals.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    draft = draft_config(sql_project_path, None, project_path)
    _proposals[key] = (stamp, draft)
    while len(_proposals) > 3:
        _proposals.pop(next(iter(_proposals)))
    return draft


def describe(draft: "Draft", state: str, destination: str, options: bool = True) -> str:
    """The proposed structure, in the words of the user, with what they can do about it."""
    from mcp_data_platform_toolkit.sql_project.config import LineageConfig
    from mcp_data_platform_toolkit.sql_project.project import layer_coverage, load_unconfigured_lineage, with_layers
    project = draft.project
    base = load_unconfigured_lineage(project.sqlproj, draft.default_schema)
    placed, total = layer_coverage(with_layers(base, LineageConfig(path="", default_schema=draft.default_schema)))
    own = "an empty lineage configuration" if state == "blank" else "no lineage configuration"
    lines = [f"The database project '{project.name}' has {own}, and the standard layers (bronze, silver, "
             f"gold) place {placed} of its {total} objects, so its lineage would show almost nothing.",
             "This is the structure inferred from its SQL"
             + (f" and from the Power BI model '{draft.model}'" if draft.model else "") + ":", ""]

    by_name = {info.name.lower(): info for info in draft.schemas}

    def count(schema: str) -> int:
        info = by_name.get(schema.lower())
        return sum(info.objects.values()) if info else 0

    lines.append("Layers, in flow order:")
    for number, (layer, members) in enumerate(draft.layers, 1):
        lines.append(f"  {number}. {layer}: {', '.join(f'{m} ({count(m)})' for m in members)}")
    if draft.ignore:
        lines.append(f"Left out, no data flows through them: {', '.join(draft.ignore)}")
    if draft.rules:
        lines.append("Loads the SQL doesn't show (rules, each an expression on 'schema.object'):")
        for rule in draft.rules[:_SHOWN_RULES]:
            lines.append(f"  - {rule.name}: {rule.match}  <-  {rule.source}"
                         + ("  (a system outside the project)" if rule.external else ""))
        if len(draft.rules) > _SHOWN_RULES:
            lines.append(f"  ... and {len(draft.rules) - _SHOWN_RULES} more")
    if draft.technical:
        lines.append(f"Columns created by the ETL: {', '.join(draft.technical[:6])}"
                     + (" ..." if len(draft.technical) > 6 else ""))

    points = [q for q in draft.questions if not q.key.startswith("load.")]
    unexplained = [q for q in draft.questions if q.key.startswith("load.")]
    if points or unexplained:
        lines += ["", "To confirm (kept at the proposed answer unless you change it):"]
        for question in points[:_SHOWN_POINTS]:
            lines.append(f"  - {question.key}: {question.text} Proposed: {question.proposed}.")
        if len(points) > _SHOWN_POINTS:
            lines.append(f"  ... and {len(points) - _SHOWN_POINTS} more")
        if unexplained:
            lines.append(f"  - {len(unexplained)} table(s) that nothing in the SQL or in these rules loads.")
    if options:
        lines += ["",
                  f"save: keep this structure as yours. It is saved in {destination}, in your folder, outside "
                  f"the repository, and used from now on.",
                  "change: write below what to change; nothing is saved until you confirm the new one.",
                  "not now: use it only for this session, without saving it."]
    return "\n".join(lines)


def _form_message(sql_project_path: str, project_path: str, state: str) -> str:
    project, destination = _paths(sql_project_path)
    return describe(propose(sql_project_path, project_path), state, destination)


# ---------------------------------------------------------------------------
# The question
# ---------------------------------------------------------------------------

def _ask(ctx, sql_project_path: str, project_path: str, config_path: str):
    from mcp_data_platform_toolkit.sql_project.project import structure_asked
    if not elicitation_supported(ctx):
        return None
    try:
        state = structure_state(sql_project_path, config_path)
        if not state:
            return None
        project, _destination = _paths(sql_project_path)
        if structure_asked(project.sqlproj):
            return None
        return Elicit(_form_message(sql_project_path, project_path, state), StructureAnswer)
    except (LookupError, ValueError, OSError, ImportError):
        return None                                     # the tool reports what is wrong with the project


def ask_sql_structure(ctx: Context, sql_project_path: str = "", config_path: str = ""):
    """Resolver of `SqlStructure`: the structure of a database project, asked only when it has none."""
    return _ask(ctx, sql_project_path, "", config_path)


def ask_model_structure(ctx: Context, project_path: str = "", sql_project_path: str = "",
                        config_path: str = ""):
    """Resolver of `ModelStructure`: as `ask_sql_structure`, with the Power BI model that reads the database."""
    return _ask(ctx, sql_project_path, project_path, config_path)


# A tool parameter of these types is filled by the user's answer, never by the
# language model: MCPServer leaves it out of the tool's input schema.
SqlStructure = Annotated[ElicitationResult[StructureAnswer], Resolve(ask_sql_structure)]
ModelStructure = Annotated[ElicitationResult[StructureAnswer], Resolve(ask_model_structure)]


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------

def settle_structure(sql_project_path: str, project_path: str = "", config_path: str = "",
                     answer=None) -> tuple[str, str]:
    """
    Applies the outcome of the question before the lineage is read. Returns
    (stop, notice): `stop` is the whole response when the lineage must not
    run yet ("" to go on), `notice` what to tell the user before it.
    """
    from mcp_data_platform_toolkit.sql_project.project import (mark_structure_asked, set_provisional_config,
                                                               structure_asked)
    try:
        state = structure_state(sql_project_path, config_path)
        if not state:
            return "", ""
        project, destination = _paths(sql_project_path)
        draft = propose(sql_project_path, project_path)
    except (LookupError, ValueError, OSError, ImportError):
        return "", ""                                   # the tool reports what is wrong with the project

    data = getattr(answer, "data", None)
    if isinstance(data, StructureAnswer):
        mark_structure_asked(project.sqlproj)
        if data.decision == "save":
            return "", _save(project, draft, destination)
        if data.decision == "change":
            return _change_message(sql_project_path, project_path, data.changes), ""
        set_provisional_config(project.sqlproj, draft.text)
        return "", ""
    if isinstance(answer, (DeclinedElicitation, CancelledElicitation)) or structure_asked(project.sqlproj):
        mark_structure_asked(project.sqlproj)
        set_provisional_config(project.sqlproj, draft.text)
        return "", ""

    # No form: the assistant asks, once. The lineage then uses the proposal as a provisional one.
    mark_structure_asked(project.sqlproj)
    set_provisional_config(project.sqlproj, draft.text)
    return _ask_through_assistant(draft, state, destination, sql_project_path, project_path), ""


def _save(project, draft: "Draft", destination: str) -> str:
    from mcp_data_platform_toolkit.sql_project.config import ConfigError, parse_config, personal_config_path
    from mcp_data_platform_toolkit.sql_project.project import forget_provisional_config
    text = draft.text
    try:
        parse_config(text, "lineage.yaml (proposed)")
        folder_notice = settle_first_choice(None)
        path = personal_config_path(project.folder, project.sqlproj)
        if os.path.isfile(path):                        # one that says nothing: its comments are kept
            previous = Path(path).read_text(encoding="utf-8-sig")
            if previous.strip():
                text += ("\n# What this file held before the structure above replaced it:\n"
                         + "".join(f"# {line}\n" for line in previous.splitlines()))
        user_folder.write_text(Path(path), text.rstrip("\n") + "\n")
    except (ConfigError, OSError) as exc:
        from mcp_data_platform_toolkit.sql_project.project import set_provisional_config
        set_provisional_config(project.sqlproj, draft.text)
        return (f"[!] The structure could not be saved ({exc}). It is used for this session only; "
                f"save it with save_lineage_config or in the file that "
                f"'mcp-data-platform-toolkit --lineage-config' opens.")
    forget_provisional_config(project.sqlproj)
    open_points = len([q for q in draft.questions if not q.key.startswith("load.")])
    message = (f"Saved the structure of '{project.name}' as '{path}' (yours, outside the repository). "
               f"It is used from now on"
               + (f"; {open_points} point(s) stayed at their proposed answer: draft_lineage_config "
                  f"lists them." if open_points else ".")
               + " Check it with test_lineage_config.")
    return message + (f"\n{folder_notice}" if folder_notice else "")


def _change_message(sql_project_path: str, project_path: str, changes: str) -> str:
    wanted = changes.strip() or "(nothing written)"
    return (
        "Nothing was run. The user was shown the structure inferred for this database project and "
        f"asked to change it: \"{wanted}\". Call draft_lineage_config with sql_project_path"
        + (" and project_path" if project_path else "")
        + ", apply their changes through 'answers' (schema.<schema> takes a layer or 'ignore', "
          "layer_order, rule.<name> yes/no, load.<schema.table>), show them the new draft and ask "
          "them to confirm it. Save it with save_lineage_config (full access mode) or, in "
          "read-only mode, give them the YAML to paste in the file that 'mcp-data-platform-toolkit "
          "--lineage-config' opens. Then call this tool again. Until it is saved the lineage uses "
          "the first proposal as a provisional structure, marked as such.")


def _ask_through_assistant(draft: "Draft", state: str, destination: str, sql_project_path: str,
                           project_path: str) -> str:
    return "\n".join([
        "Nothing was run yet. " + describe(draft, state, destination, options=False),
        "",
        "Show this structure to the user and ask whether to keep it or change it. If they keep it, "
        "save it with save_lineage_config using the YAML below (full access mode); in read-only "
        "mode give them the YAML to paste in the file that 'mcp-data-platform-toolkit "
        "--lineage-config' opens. If they want changes, apply them with draft_lineage_config "
        "(answers) and show the new draft. Then call this tool again: until the structure is saved "
        "the lineage uses this one as a provisional structure, marked as such in every response. "
        "This question is not asked again.",
        "", "```yaml", *draft.text.rstrip("\n").split("\n"), "```"])
