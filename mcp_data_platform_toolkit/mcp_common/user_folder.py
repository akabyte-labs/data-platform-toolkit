"""
The user's own folder: where the server keeps the files that belong to the
person using it, not to the projects (their rules for the descriptions,
their lineage configurations).

    <the folder>\\
      settings.json                    (only in the default folder) the folder chosen,
                                       and whether the user was asked for their rules
      description_guidelines.md        the rules for every project
      projects\\
        <repository>\\                  everything of one project, named after its repository
          description_guidelines.md
          <database project>.lineage.yaml

It is outside every repository on purpose: a project folder is often a
customer's repository, where a personal file must not end up by mistake.
By default it is %APPDATA%\\data-platform-toolkit on Windows and
~/.config/data-platform-toolkit elsewhere; the user may choose another one
(the first time, or later with `mcp-data-platform-toolkit --user-folder`),
and MCP_USER_FOLDER, when set, wins over the choice.

The language model never chooses an arbitrary folder: a prompt injection in
a model description could otherwise point the server's writes anywhere. A
tool can only switch between the two standard folders (LOCATIONS); any
other folder is typed by the user. No MCP dependency here: the database
project lineage uses it too.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from mcp_data_platform_toolkit.semantic_model.tmdl_io import write_tmdl_lines

APP_FOLDER_NAME = "data-platform-toolkit"
ENV_FOLDER = "MCP_USER_FOLDER"
SETTINGS_FILE = "settings.json"
PROJECTS_FOLDER = "projects"
# The one file at the top of the folder: the user's rules for every project.
GENERAL_RULES_FILE = "description_guidelines.md"

# The folders a tool (that is, the language model) may choose.
LOCATIONS = ("settings", "documents")

# Levels above a project folder searched for the root of its repository.
MAX_REPO_DEPTH = 12

# Characters Windows refuses in a file name, and control characters.
_UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# Where the folder is
# ---------------------------------------------------------------------------

def default_folder() -> Path:
    """%APPDATA%\\data-platform-toolkit on Windows, ~/.config/data-platform-toolkit elsewhere."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_FOLDER_NAME


def _windows_documents() -> Path | None:
    """
    The Documents folder as Windows knows it. With OneDrive folder backup it
    is not ~/Documents but a folder inside OneDrive: only the known-folder
    API says which.
    """
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class Guid(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        folder_id = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")   # FOLDERID_Documents
        guid = Guid(folder_id.fields[0], folder_id.fields[1], folder_id.fields[2],
                    (ctypes.c_ubyte * 8)(*folder_id.bytes[8:]))
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)):
            return None
        try:
            return Path(path.value) if path.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(path)
    except Exception:  # noqa: BLE001 - any failure falls back to ~/Documents
        return None


def documents_folder() -> Path:
    """Documents\\data-platform-toolkit: where the files are easier to open."""
    documents = _windows_documents() if os.name == "nt" else None
    return (documents or Path.home() / "Documents") / APP_FOLDER_NAME


def location_folder(location: str) -> Path:
    """The folder of one of the two standard locations."""
    if location == "settings":
        return default_folder()
    if location == "documents":
        return documents_folder()
    raise ValueError(f"'location' must be one of: {', '.join(LOCATIONS)} (got '{location}').")


def _settings_path() -> Path:
    return default_folder() / SETTINGS_FILE


def _read_settings() -> dict:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings(data: dict) -> None:
    write_text(_settings_path(), json.dumps(data, indent=2) + "\n")


def setting(name: str):
    """One of the user's settings (settings.json), None when it was never recorded."""
    return _read_settings().get(name)


def remember(name: str, value) -> None:
    """Records one of the user's settings, keeping the others."""
    settings = _read_settings()
    settings[name] = value
    _write_settings(settings)


def configured_folder() -> tuple[Path | None, str]:
    """(the user's folder, where it comes from), or (None, ...) before the first choice."""
    from_env = os.environ.get(ENV_FOLDER, "").strip()
    if from_env:
        return Path(from_env).expanduser(), f"set by {ENV_FOLDER}"
    chosen = _read_settings().get("folder")
    if isinstance(chosen, str) and chosen.strip():
        return Path(chosen), "chosen"
    return None, "not chosen yet"


def user_folder() -> Path:
    """The user's folder: the chosen one, or the default."""
    folder, _source = configured_folder()
    return folder or default_folder()


def needs_choice() -> bool:
    """True until the user chose a folder (or MCP_USER_FOLDER names one)."""
    return configured_folder()[0] is None


def _refuse_when_set_by_environment() -> None:
    if os.environ.get(ENV_FOLDER, "").strip():
        raise ValueError(f"Your folder is set by {ENV_FOLDER}: change it there.")


def absolute_folder(path: str | Path) -> Path:
    text = str(path or "").strip().strip('"')
    if not text:
        raise ValueError("The folder cannot be empty.")
    folder = Path(text).expanduser()
    if not folder.is_absolute():
        raise ValueError(f"'{text}' is not an absolute path: give the whole path of the folder.")
    return folder


def choose_folder(folder: str | Path) -> Path:
    """Records the user's folder and creates it. A later change moves the files with `move_folder`."""
    _refuse_when_set_by_environment()
    target = absolute_folder(folder)
    target.mkdir(parents=True, exist_ok=True)
    settings = _read_settings()
    settings["folder"] = str(target)
    _write_settings(settings)
    return target


def move_folder(folder: str | Path, top_files: tuple[str, ...] = (GENERAL_RULES_FILE,)) -> list[str]:
    """
    Moves the user's files to another folder and remembers it: the
    `top_files` of the folder itself and everything under projects/. Nothing
    else is touched (a folder the user typed may hold their own files), and
    nothing is overwritten: a file already in the new folder is kept, and
    the old one stays where it was.
    """
    _refuse_when_set_by_environment()
    target = absolute_folder(folder)
    source = user_folder()
    if target.resolve() == source.resolve() and not needs_choice():
        return [f"Your files are already in '{target}'."]
    files = [source / name for name in top_files]
    projects = source / PROJECTS_FOLDER
    files += sorted(path for path in projects.rglob("*") if path.is_file()) if projects.is_dir() else []
    messages = []
    for path in files:
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        destination = target / relative
        if destination.exists():
            messages.append(f"Kept '{destination}', already there; '{path}' was not moved.")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
        messages.append(f"Moved '{relative.as_posix()}'.")
    choose_folder(target)
    messages.append(f"Your files are now in '{target}'.")
    return messages


# ---------------------------------------------------------------------------
# The folder of one project
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectIdentity:
    """Which project a folder belongs to, for the user's files about it."""

    name: str
    folder: Path                # the project's own folder (where the .pbip or the .sqlproj is)
    repo_root: Path | None      # the root of its Git repository, if any


def repository_root(folder: Path) -> Path | None:
    current = folder
    for _ in range(MAX_REPO_DEPTH):
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def safe_file_name(name: str) -> str:
    """A name usable as a file or folder name on every system."""
    text = _UNSAFE_NAME_RE.sub("_", str(name or "")).strip().rstrip(". ")
    return text or "project"


def project_identity(folder: str | Path) -> ProjectIdentity:
    """The project of `folder`, named after its repository (or the folder itself)."""
    path = Path(folder)
    root = repository_root(path)
    return ProjectIdentity(name=(root or path).name, folder=path, repo_root=root)


def project_dir(project: ProjectIdentity) -> Path:
    """projects/<repository> in the user's folder: everything of the user's about that project."""
    return user_folder() / PROJECTS_FOLDER / safe_file_name(project.name)


def ensure_project_dir(project: ProjectIdentity) -> Path:
    """
    projects/<repository>, created as soon as a tool works on the project, so
    the user always finds where its files go. Not before the folder is chosen
    (it would settle the choice in the default folder), and never failing the
    call that needed it.
    """
    path = project_dir(project)
    if not needs_choice():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return path


def write_text(path: Path, text: str) -> None:
    """Writes one of the user's files, atomically, creating its folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_tmdl_lines(str(path), text.splitlines(keepends=True))
