"""
Where things are in a PBIP project: path confinement, resolution of the
semantic model folder from a .pbip file or a folder, the report folders.

Every path the tools receive comes from a language model: the names it
contains are validated here, before any file is opened.
"""

from __future__ import annotations

import json
import os
import threading
import time

from mcp_data_platform_toolkit.mcp_common.errors import AmbiguousModelError
from mcp_data_platform_toolkit.semantic_model.tmdl_io import extended_path


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------
# Table, column and role names come from a language model and used to end up
# in `os.path.join` without any check: a `table_name` such as
# '../../../../secret' escaped the model folder and was read, or REWRITTEN by
# the write tools. No malicious user is needed: a hallucinated name or a
# prompt injection inside a model description is enough.


class PathNotAllowedError(ValueError):
    """An element name tried to escape the folder that contains it."""


def _is_inside(folder: str, path: str) -> bool:
    """True if `path` really lies inside `folder` (normalized comparison)."""
    try:
        root = os.path.realpath(folder)
        candidate = os.path.realpath(path)
        return os.path.commonpath([root, candidate]) == root
    except (OSError, ValueError):
        # commonpath raises ValueError on different drives (C: vs D:):
        # a different drive means outside the folder anyway.
        return False


def simple_name(name: str, description: str) -> str:
    """
    File/element name without path components.

    Rejects separators, '..' and Windows drives. It is the first-level check:
    the second level is `confined_path`, which verifies the result after
    symbolic links are resolved.
    """
    text = str(name or "").strip()
    if not text:
        raise ValueError(f"{description} cannot be empty.")
    if "/" in text or "\\" in text or "\x00" in text:
        raise PathNotAllowedError(
            f"{description} '{text}' contains a path separator: "
            f"only the element name is allowed, not a path."
        )
    if text in (".", "..") or ":" in text:
        raise PathNotAllowedError(
            f"{description} '{text}' is not a valid element name."
        )
    return text


def confined_path(folder: str, path: str, description: str) -> str:
    """Returns `path` only if it stays inside `folder`, otherwise raises."""
    if not _is_inside(folder, path):
        raise PathNotAllowedError(
            f"{description} would lead outside '{folder}'. Operation refused."
        )
    return path


# ---------------------------------------------------------------------------
# Project path resolution
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Allowed roots (optional)
# ---------------------------------------------------------------------------
# The confinement above keeps element NAMES inside the project, but the
# project path itself is chosen by the language model: in full access mode a
# prompt injection hidden in a model description could point the write tools
# at any other project on the disk. MCP_ALLOWED_ROOTS, when set, is the list
# of folders (separated by os.pathsep: ';' on Windows, ':' elsewhere) the
# server may read or write. Unset, nothing changes.

def allowed_roots() -> list[str]:
    """The folders of MCP_ALLOWED_ROOTS, or [] when every path is allowed. Read on every call."""
    raw = os.environ.get("MCP_ALLOWED_ROOTS", "")
    return [os.path.realpath(part.strip()) for part in raw.split(os.pathsep) if part.strip()]


def check_allowed_root(path: str, description: str = "The path") -> str:
    """Returns `path` if it lies inside one of the allowed roots (or none is set), otherwise raises."""
    roots = allowed_roots()
    if roots and not any(_is_inside(root, path) for root in roots):
        raise PathNotAllowedError(
            f"{description} '{path}' is outside the folders this server may access "
            f"(MCP_ALLOWED_ROOTS: {os.pathsep.join(roots)}). Operation refused."
        )
    return path


def check_output_path(path: str, project_folders: list[str], description: str) -> str:
    """
    Returns `path`, a file a tool writes outside the model (a lineage
    export), if it lies inside the allowed roots when MCP_ALLOWED_ROOTS is
    set, otherwise inside one of `project_folders`; raises if it doesn't.

    The path is chosen by the language model: without this, in full access
    mode with no allowed roots, an export could be written anywhere on the
    disk, a startup folder included.
    """
    if allowed_roots():
        return check_allowed_root(path, description)
    if not any(_is_inside(folder, path) for folder in project_folders):
        shown = ", ".join(f"'{folder}'" for folder in project_folders)
        raise PathNotAllowedError(
            f"{description} '{path}' is outside the project folders ({shown}). Operation "
            f"refused: save it inside one of them, or add its folder to MCP_ALLOWED_ROOTS."
        )
    return path


def _resolve_project_folder(path: str) -> str:
    """Common preamble: accepts a .pbip file or a project folder."""
    if not path:
        raise ValueError("The project path cannot be empty.")
    if os.path.isfile(path) and path.lower().endswith(".pbip"):
        return check_allowed_root(os.path.dirname(os.path.abspath(path)))
    if os.path.isdir(path):
        return check_allowed_root(os.path.abspath(path))
    raise FileNotFoundError(
        f"'{path}' is neither an existing .pbip file nor a valid folder."
    )


# The search for a project folder must never turn into a scan of the whole
# disk: a path pasted by mistake (the OneDrive root, C:\) hung the MCP client
# for minutes, without any message. A PBIP project keeps '.SemanticModel' and
# '.Report' a few levels below the given root, so past this depth there is
# nothing to find.
MAX_SEARCH_DEPTH = 6

SEARCH_EXCLUDED_DIRS = {
    ".git", ".vs", ".vscode", ".idea", "node_modules", "__pycache__",
    "bin", "obj", ".venv", "venv", "env", ".pytest_cache", ".mypy_cache",
}


def _bounded_walk(root: str, max_depth: int = MAX_SEARCH_DEPTH):
    """Pruned `os.walk`: no service folders, no endless descent."""
    root = os.path.abspath(root)
    root_level = root.rstrip(os.sep).count(os.sep)
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d.lower() not in SEARCH_EXCLUDED_DIRS)
        if current.rstrip(os.sep).count(os.sep) - root_level >= max_depth:
            dirs[:] = []
        yield current, dirs, files


# The search walk used to run again on EVERY call of EVERY tool: the chain
# read_table_lines -> get_table_file_path -> find_tables_folder ->
# find_definition_folder always ends up here. On a broad root (OneDrive, a
# workspace with dozens of projects) that is a scan of thousands of folders,
# repeated identically dozens of times for a single user request.
# The cache has a short TTL: the structure of a PBIP project doesn't change
# during a conversation, and if it does it realigns by itself within seconds.
_CACHE_TTL_SECONDS = 30.0
_semantic_model_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def clear_path_cache() -> None:
    """Invalidates the path resolution cache (used by tests and after a write)."""
    with _cache_lock:
        _semantic_model_cache.clear()


def find_semantic_model_folder(project_path: str) -> str:
    """Finds the '*.SemanticModel' folder starting from the .pbip file or the project folder."""
    key = os.path.normcase(os.path.abspath(str(project_path or "")))
    now = time.monotonic()
    with _cache_lock:
        entry = _semantic_model_cache.get(key)
    if entry is not None and now - entry[0] < _CACHE_TTL_SECONDS and os.path.isdir(entry[1]):
        return check_allowed_root(entry[1], "The semantic model")

    resolved = _find_semantic_model_folder(project_path)
    with _cache_lock:
        _semantic_model_cache[key] = (now, resolved)
    # Checked on the RESULT too: a .pbip, or the report it names, can point to
    # a model folder outside the given path.
    return check_allowed_root(resolved, "The semantic model")


def _read_json_object(path: str) -> dict:
    """A small JSON file whose top level is an object; {} if it can't be read."""
    try:
        with open(extended_path(path), "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def read_dataset_reference(report_folder: str) -> dict:
    """
    The `datasetReference` of a PBIR report ('definition.pbir'): `byPath` to
    a local model or `byConnection` to a published one; {} if unreadable.
    The single reader of that file, for model resolution and report tools.
    """
    reference = _read_json_object(os.path.join(report_folder, "definition.pbir")).get("datasetReference")
    return reference if isinstance(reference, dict) else {}


def pbir_model_path(report_folder: str) -> str:
    """
    Folder of the semantic model a PBIR report is bound to by path
    ('definition.pbir', `datasetReference.byPath`), or "" if it isn't.
    """
    by_path = read_dataset_reference(report_folder).get("byPath")
    path = by_path.get("path") if isinstance(by_path, dict) else None
    if not isinstance(path, str) or not path.strip():
        return ""
    return os.path.normpath(os.path.join(report_folder, path.strip()))


def _model_folder_of_pbip(pbip_path: str) -> tuple[str, bool]:
    """
    (semantic model folder of a .pbip file or "", whether the file declares
    its artifacts).

    A .pbip file names its own artifacts: the model directly, or the report,
    whose 'definition.pbir' points to the model by path. Without this the
    .pbip only stood for the folder that contains it: with two projects side
    by side (the usual layout of a PBIP repository) the tool answered "which
    model?", and the question itself advised passing the .pbip file again.
    """
    folder = os.path.dirname(os.path.abspath(pbip_path))
    candidates: list[str] = []
    declared = False
    artifacts = _read_json_object(pbip_path).get("artifacts")
    for artifact in artifacts if isinstance(artifacts, list) else []:
        if not isinstance(artifact, dict):
            continue
        for key, entry in artifact.items():
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str) or not path.strip():
                continue
            declared = True
            target = os.path.normpath(os.path.join(folder, path.strip()))
            if key.lower() == "report":
                target = pbir_model_path(target)
            if target:
                candidates.append(target)
    # Power BI Desktop names the folders after the .pbip file.
    stem = os.path.splitext(os.path.basename(pbip_path))[0]
    candidates.append(os.path.join(folder, f"{stem}.SemanticModel"))

    for candidate in candidates:
        if candidate.endswith(".SemanticModel") and os.path.isdir(candidate):
            return candidate, declared
    return "", declared


def _find_semantic_model_folder(project_path: str) -> str:
    project_folder = _resolve_project_folder(project_path)

    if project_folder.endswith(".SemanticModel"):
        return project_folder

    if os.path.isfile(project_path) and project_path.lower().endswith(".pbip"):
        own_model, declared = _model_folder_of_pbip(project_path)
        if own_model:
            return own_model
        if declared:
            # Picking a model from the surrounding folder would answer about
            # ANOTHER project: a report connected to a published model has
            # no local model at all.
            raise FileNotFoundError(
                f"'{os.path.basename(project_path)}' has no local semantic model: its report "
                f"is connected to a published model, or the '.SemanticModel' folder it "
                f"refers to is missing. Pass the path of the '.SemanticModel' folder to analyze."
            )

    candidates: list[str] = []
    for root, dirs, _files in _bounded_walk(project_folder):
        for name in dirs:
            if name.endswith(".SemanticModel"):
                candidates.append(os.path.join(root, name))

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # This used to return the first folder os.walk met: with a workspace
        # containing several PBIP projects the chosen model was arbitrary and
        # nobody noticed. It is not a user error though, but an incomplete
        # request, so the tool asks which model was meant (see
        # AmbiguousModelError, which the tool boundary turns into a normal
        # response instead of a failure).
        raise AmbiguousModelError(project_folder, sorted(candidates))

    # The path may point directly at the model folder
    if os.path.isdir(os.path.join(project_folder, "definition", "tables")):
        return project_folder

    raise FileNotFoundError(
        f"No '.SemanticModel' folder found under '{project_folder}'."
    )


def find_definition_folder(project_path: str) -> str:
    return os.path.join(find_semantic_model_folder(project_path), "definition")


def find_tables_folder(project_path: str) -> str:
    """The 'definition/tables' folder containing the tables' .tmdl files."""
    tables_path = os.path.join(find_definition_folder(project_path), "tables")
    if os.path.isdir(tables_path):
        return tables_path
    raise FileNotFoundError(f"TMDL tables folder does not exist: '{tables_path}'.")


def list_table_files(project_path: str) -> list[tuple[str, str]]:
    """[(table_name_from_file, file_path)] sorted by name."""
    tables_dir = find_tables_folder(project_path)
    result = []
    for file_name in sorted(os.listdir(tables_dir)):
        if file_name.lower().endswith(".tmdl"):
            result.append((os.path.splitext(file_name)[0], os.path.join(tables_dir, file_name)))
    return result


def find_report_folders(project_path: str) -> list[str]:
    """All '*.Report' (PBIR) folders reachable from the given path."""
    project_folder = _resolve_project_folder(project_path)

    if project_folder.endswith(".Report"):
        return [project_folder]

    folders = []
    for root, dirs, _files in _bounded_walk(project_folder):
        for name in dirs:
            if name.endswith(".Report"):
                folders.append(os.path.join(root, name))

    if not folders:
        raise FileNotFoundError(
            f"No '.Report' folder found under '{project_folder}' "
            f"(searched up to {MAX_SEARCH_DEPTH} levels deep). "
            f"Remember that reports live in a different project from the semantic model: "
            f"pass the workspace folder that contains the report projects."
        )
    return sorted(folders)
