"""
The files of a database project: where the project is, which .sql files
define it, how to read them.

The project is identified by its .sqlproj, never by a bare folder: scanning
every .sql under a folder pasted by mistake (a repository root, a drive)
would mix scripts that are not part of the database into its lineage.
"""

from __future__ import annotations

import fnmatch
import os
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass

from mcp_data_platform_toolkit.semantic_model.project_paths import check_allowed_root

EXCLUDED_DIRS = {"bin", "obj", ".vs", ".git", ".idea", ".vscode", "__pycache__", "node_modules"}

# A single .sql above this size is skipped, and the summary says so. The
# parser builds a full tree in memory, and on a generated script (a dump, a
# migration of hundreds of MB) that costs minutes or the memory for nothing.
MAX_SQL_FILE_BYTES = 5_000_000

# How far below the given folder a .sqlproj is looked for (as for PBIP projects).
MAX_SEARCH_DEPTH = 6

# Items of a .sqlproj whose files are not objects of the database: deployment
# scripts (they usually only include others with ':r') and files kept in the
# project without being built.
_NOT_BUILT_ITEMS = {"predeploy", "postdeploy", "none", "refactorlog"}


@dataclass(frozen=True)
class DatabaseProject:
    folder: str
    sqlproj: str
    name: str


@dataclass(frozen=True)
class SqlFile:
    path: str
    rel_path: str          # relative to the project folder, with '/'
    size: int


def find_database_project(path: str) -> DatabaseProject:
    """The database project of a .sqlproj file or of a folder that contains one."""
    text = str(path or "").strip()
    if not text:
        raise ValueError("The database project path cannot be empty.")
    full = os.path.abspath(text)
    if os.path.isfile(full):
        if not full.lower().endswith(".sqlproj"):
            raise ValueError(f"'{text}' is not a .sqlproj file: give the database project "
                             f"(.sqlproj) or its folder.")
        check_allowed_root(full, "The database project")
        return _project(full)
    if not os.path.isdir(full):
        raise FileNotFoundError(f"'{text}' is neither a .sqlproj file nor a folder.")
    check_allowed_root(full, "The database project")

    found = []
    root_level = full.rstrip(os.sep).count(os.sep)
    for current, dirs, files in os.walk(full):
        dirs[:] = sorted(d for d in dirs if d.lower() not in EXCLUDED_DIRS)
        if current.rstrip(os.sep).count(os.sep) - root_level >= MAX_SEARCH_DEPTH:
            dirs[:] = []
        found.extend(os.path.join(current, f) for f in sorted(files) if f.lower().endswith(".sqlproj"))
        if current == full and found:
            break                           # a project in the folder itself wins
    if not found:
        raise FileNotFoundError(
            f"No database project (.sqlproj) found under '{text}'. Give the folder of the "
            f"SQL Server database project or its .sqlproj file.")
    if len(found) > 1:
        listed = "\n".join(f"  - {p}" for p in found[:10])
        raise ValueError(
            f"Under '{text}' there are {len(found)} database projects:\n{listed}\n"
            f"Ask the user which one they mean and repeat the call with its .sqlproj file.")
    return _project(found[0])


def _project(sqlproj: str) -> DatabaseProject:
    return DatabaseProject(folder=os.path.dirname(sqlproj), sqlproj=sqlproj,
                           name=os.path.splitext(os.path.basename(sqlproj))[0])


def _excluded_patterns(sqlproj: str) -> list[str]:
    """Relative paths (or globs) of the files the project doesn't build, lowercase with '/'."""
    try:
        root = ElementTree.parse(sqlproj).getroot()
    except (ElementTree.ParseError, OSError):
        return []                           # an unreadable project file excludes nothing
    patterns = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        target = None
        if tag in _NOT_BUILT_ITEMS:
            target = element.get("Include")
        elif tag == "build":
            target = element.get("Remove")
        if target:
            patterns.extend(p.strip().replace("\\", "/").lower() for p in target.split(";") if p.strip())
    return patterns


def project_default_schema(sqlproj: str) -> str:
    """The <DefaultSchema> of the .sqlproj (the schema of names written without one), or ''."""
    try:
        root = ElementTree.parse(sqlproj).getroot()
    except (ElementTree.ParseError, OSError):
        return ""
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() == "defaultschema" and (element.text or "").strip():
            return element.text.strip()
    return ""


def discover_sql_files(project: DatabaseProject) -> tuple[list[SqlFile], list[str]]:
    """(the .sql files that define the database, the ones the .sqlproj excludes)."""
    patterns = _excluded_patterns(project.sqlproj)
    files, excluded = [], []
    for current, dirs, names in os.walk(project.folder):
        dirs[:] = sorted(d for d in dirs if d.lower() not in EXCLUDED_DIRS)
        for name in sorted(names):
            if not name.lower().endswith(".sql"):
                continue
            path = os.path.join(current, name)
            rel = os.path.relpath(path, project.folder).replace("\\", "/")
            if any(fnmatch.fnmatchcase(rel.lower(), p) for p in patterns):
                excluded.append(rel)
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            files.append(SqlFile(path=path, rel_path=rel, size=size))
    return files, excluded


def read_sql_text(path: str) -> str:
    """
    The text of a .sql file. SQL Server tools save UTF-8 with or without BOM,
    UTF-16 (SSMS "Unicode") or the Windows code page: the BOM decides, then
    UTF-8, then cp1252, which accepts any byte.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")
