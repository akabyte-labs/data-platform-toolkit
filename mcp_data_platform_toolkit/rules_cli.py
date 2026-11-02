"""
The terminal commands for the user's own files (see `user_folder`):

    mcp-data-platform-toolkit --guidelines [project]
        Opens the rules for the descriptions (for every project, or for the
        given one) in the system's editor, creating the file from its
        template.

    mcp-data-platform-toolkit --lineage-config <database project>
        Opens the lineage configuration of a database project: the
        repository's when it has one, otherwise the user's own, created from
        a draft of the project when missing.

    mcp-data-platform-toolkit --user-folder [folder]
        Shows where the user's files are and moves them: to the folder given,
        or to the one chosen from a short menu.

They are the way a person reaches and moves those files without knowing
where they are. Unlike the MCP tools they may choose any folder: whoever
types in this terminal is the user, not the language model.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from mcp_data_platform_toolkit.lineage.draft_notice import DRAFT_WARNING, HOW_TO_CHECK, is_generated
from mcp_data_platform_toolkit.mcp_common import user_folder
from mcp_data_platform_toolkit.mcp_common.errors import AmbiguousModelError
from mcp_data_platform_toolkit.semantic_model import description_rules as rules

Ask = Callable[[str], str]
Say = Callable[[str], None]


def _launch(command: list[str]) -> bool:
    """
    Starts `command` without waiting for it: an opener that hands the file to a
    running editor returns at once, one that stays until the editor closes
    must not hold the caller. False when it can't start or fails at once.
    """
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
    except OSError:
        return False
    try:
        return process.wait(timeout=2) == 0
    except subprocess.TimeoutExpired:
        return True                         # still running: it is the editor, or on its way to it


def open_in_editor(path: Path) -> bool:
    """
    Opens `path` with the program the system associates with it, without
    waiting for it; False if none could (no graphical session included).
    """
    try:
        if os.name == "nt":
            try:
                os.startfile(str(path))     # the user's own file, opened at their request
            except OSError:                 # no program associated with the extension
                return _launch(["notepad.exe", str(path)])
            return True
        if sys.platform == "darwin":
            return _launch(["open", str(path)])
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False                    # no graphical session: a server on a remote machine
        return _launch(["xdg-open", str(path)])
    except OSError:
        return False


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask_folder(ask: Ask, say: Say, keep: bool) -> Path | None:
    """The folder chosen from the menu, or None to leave the files where they are."""
    say("Where should your files (rules for the descriptions, lineage configurations) be kept?")
    say(f"  1. the settings folder: {user_folder.default_folder()} (recommended)")
    say(f"  2. Documents: {user_folder.documents_folder()} (easier to open)")
    say("  3. another folder")
    if keep:
        say("  4. leave them where they are")
    default, last = ("4", 4) if keep else ("1", 3)
    while True:
        answer = (ask(f"Choice [{default}]: ") or default).strip()
        if answer == "1":
            return user_folder.default_folder()
        if answer == "2":
            return user_folder.documents_folder()
        if answer == "3":
            folder = ask("Whole path of the folder: ").strip().strip('"')
            if folder and Path(folder).expanduser().is_absolute():
                return Path(folder).expanduser()
            say("Give the whole path, for example C:\\Users\\me\\Documents\\Rules.")
            continue
        if keep and answer == "4":
            return None
        say(f"Answer with a number from 1 to {last}.")


def _settle_in_terminal(ask: Ask, say: Say, interactive: bool) -> None:
    """The first-time choice, asked here when nobody answered it yet."""
    if not user_folder.needs_choice():
        return
    folder = _ask_folder(ask, say, keep=False) if interactive else user_folder.default_folder()
    user_folder.choose_folder(folder)
    rules.ensure_general_file()
    say(f"Your files are kept in '{folder}'.")


def _open(path: Path, what: str, say: Say, open_file: bool) -> int:
    say(f"{what}: {path}")
    if open_file and not open_in_editor(path):
        say("Open it with any text editor: it is read again at every call, no restart needed.")
    return 0


def run_guidelines(project_path: str = "", ask: Ask = input, say: Say = print,
                   interactive: bool | None = None, open_file: bool = True) -> int:
    """`--guidelines [project]`."""
    interactive = _interactive() if interactive is None else interactive
    try:
        _settle_in_terminal(ask, say, interactive)
        if project_path:
            project = rules.project_rules(project_path)
            path = rules.project_file(project)
            template = rules.PROJECT_TEMPLATE.format(name=project.name)
        else:
            path, template = rules.general_file(), rules.GENERAL_TEMPLATE
        if not path.exists():
            user_folder.write_text(path, template)
            say(f"Created '{path}' from its template.")
    except AmbiguousModelError as exc:
        say(exc.question())
        return 1
    except (OSError, ValueError) as exc:
        say(f"ERROR: {exc}")
        return 1
    return _open(path, "Your rules", say, open_file)


# The user's own configuration when no draft can be made: the standard medallion.
LINEAGE_TEMPLATE = """# The lineage configuration of {name}: your own, outside its repository.
# How to write it: the guide returned by get_lineage_config_guide, or ask the
# assistant to draft it (draft_lineage_config).
version: 1
# A draft, generated automatically: check it, then remove this line (or set it to false).
draft: true
layers:
  bronze: bronze
  silver: silver
  gold: gold
"""


# Said after a configuration is generated, before it is opened.
LINEAGE_DRAFT_WARNING = (
    f"{DRAFT_WARNING} It opens now in your editor. To check it: ask the assistant to complete it "
    f"(draft_lineage_config asks only what the SQL can't tell) and run test_lineage_config. {HOW_TO_CHECK}"
)
LINEAGE_STILL_DRAFT = f"It is still a draft ('draft: true'), not checked yet. {HOW_TO_CHECK}"


def run_lineage_config(sql_project_path: str = "", ask: Ask = input, say: Say = print,
                       interactive: bool | None = None, open_file: bool = True) -> int:
    """`--lineage-config <database project>`."""
    from mcp_data_platform_toolkit.sql_project.config import find_project_config, personal_config_file
    from mcp_data_platform_toolkit.sql_project.sql_files import find_database_project

    if not sql_project_path:
        say('Give the database project: mcp-data-platform-toolkit --lineage-config "C:\\path\\to\\Project.sqlproj"')
        return 1
    interactive = _interactive() if interactive is None else interactive
    try:
        project = find_database_project(sql_project_path)
        _settle_in_terminal(ask, say, interactive)
        in_use = find_project_config(project.folder, project.sqlproj)
        if in_use:
            path = Path(in_use)
            mine = os.path.normcase(in_use) == os.path.normcase(personal_config_file(project.folder, project.sqlproj))
            try:
                if is_generated(path.read_text(encoding="utf-8-sig")):
                    say(LINEAGE_STILL_DRAFT)
            except OSError:
                pass                        # opening it says what is wrong
            return _open(path, "Your lineage configuration" if mine else
                         "The lineage configuration of the repository (it wins over yours)", say, open_file)
        path = Path(personal_config_file(project.folder, project.sqlproj))
        try:
            from mcp_data_platform_toolkit.lineage.config_draft import draft_config
            text, origin = draft_config(sql_project_path).text, "a draft of the project"
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the template
            text, origin = LINEAGE_TEMPLATE.format(name=project.name), f"the template ({exc})"
        user_folder.write_text(path, text)
        say(f"Created '{path}' from {origin}.")
        say(LINEAGE_DRAFT_WARNING)
    except (OSError, ValueError, LookupError) as exc:
        say(f"ERROR: {exc}")
        return 1
    return _open(path, "Your lineage configuration", say, open_file)


def run_user_folder(folder: str = "", ask: Ask = input, say: Say = print,
                    interactive: bool | None = None) -> int:
    """`--user-folder [folder]`."""
    interactive = _interactive() if interactive is None else interactive
    current, source = user_folder.configured_folder()
    say(f"Your folder (rules for the descriptions, lineage configurations): "
        f"{current or user_folder.default_folder()} ({source})")
    if os.environ.get(user_folder.ENV_FOLDER, "").strip():
        say(f"It is set by the environment variable {user_folder.ENV_FOLDER}: change it there.")
        return 1
    if not folder:
        if not interactive:
            say('To move it: mcp-data-platform-toolkit --user-folder "<the folder>"')
            return 0
        chosen = _ask_folder(ask, say, keep=True)
        if chosen is None:
            if user_folder.needs_choice():
                user_folder.choose_folder(user_folder.default_folder())
            say("Nothing moved.")
            return 0
        folder = str(chosen)
    try:
        for message in user_folder.move_folder(folder):
            say(message)
    except (OSError, ValueError) as exc:
        say(f"ERROR: {exc}")
        return 1
    return 0
