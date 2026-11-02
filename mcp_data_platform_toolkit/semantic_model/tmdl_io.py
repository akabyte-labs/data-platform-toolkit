"""
Reading and writing TMDL files: encoding (UTF-8, BOM preserved), line
endings (preserved), atomic writes, per-file locks and the fingerprint that
refuses to overwrite a file changed after it was read.

`write_tmdl_lines` is the ONLY write path for TMDL files.
"""

from __future__ import annotations

import os
import tempfile
import threading

from mcp_data_platform_toolkit.semantic_model.tmdl_parser import TmdlError


# Power BI Desktop writes TMDL files with CRLF terminators. Reading here uses
# universal newlines, so in memory every line ends with '\n': without
# translating them back on write, every edit would convert the whole file to
# LF, rewriting lines nobody touched.
DEFAULT_TMDL_EOL = "\r\n"


def _dominant_eol(data: bytes) -> str | None:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    if crlf and crlf >= lf:
        return "\r\n"
    if lf:
        return "\n"
    return None


def detect_eol(file_path: str) -> str:
    """
    Line terminator to use for `file_path`: the file's own if it exists,
    otherwise that of a nearby .tmdl file (a new file must be consistent with
    the rest of the model), otherwise CRLF.
    """
    try:
        with open(extended_path(file_path), "rb") as handle:
            eol = _dominant_eol(handle.read(65536))
        if eol:
            return eol
    except OSError:
        pass

    folder = os.path.dirname(file_path)
    try:
        neighbours = sorted(n for n in os.listdir(folder) if n.lower().endswith(".tmdl"))
    except OSError:
        neighbours = []
    for name in neighbours:
        neighbour = os.path.join(folder, name)
        if os.path.abspath(neighbour) == os.path.abspath(file_path):
            continue
        try:
            with open(extended_path(neighbour), "rb") as handle:
                eol = _dominant_eol(handle.read(65536))
            if eol:
                return eol
        except OSError:
            continue

    return DEFAULT_TMDL_EOL


# Power BI Desktop and Windows editors often write .tmdl files with a UTF-8
# BOM. Read with plain 'utf-8', the BOM stays attached to the first character:
# '\ufefftable Sales' is no longer a recognizable declaration, the `table`
# block becomes invisible and `load_semantic_model` silently falls back to the
# FILE name, mislabeling every lineage edge. Files are therefore read with
# 'utf-8-sig', and whether the BOM was there is remembered so it can be
# written back as it was: removing it would be an unrequested change to a
# file the user keeps under version control.


def extended_path(path: str) -> str:
    """
    Path in the Windows extended form ('\\\\?\\...'), which bypasses MAX_PATH.

    Needed because the atomic write creates a temporary file whose name is
    longer than the final one: on a .tmdl path of 249 characters (within the
    260 limit) the temporary file was 267 characters long and the write failed
    with a FileNotFoundError pointing at a temporary file the user had never
    seen, while the real file was perfectly writable.

    Not a lab case: it is the normal shape of an enterprise PBIP project,
    living in a synced OneDrive folder ('OneDrive - Company Name') with a deep
    structure (<workspace>\\<Project>.SemanticModel\\definition\\tables\\<Table>.tmdl).
    """
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):          # UNC: \\server\share
        return "\\\\?\\UNC" + absolute[1:]
    return "\\\\?\\" + absolute


def _had_bom(file_path: str) -> bool:
    try:
        with open(extended_path(file_path), "rb") as handle:
            return handle.read(3) == b"\xef\xbb\xbf"
    except OSError:
        return False


def read_tmdl_lines(file_path: str) -> list[str]:
    try:
        with open(extended_path(file_path), "r", encoding="utf-8-sig") as handle:
            return handle.readlines()
    except UnicodeDecodeError as exc:
        raise TmdlError(
            f"File '{os.path.basename(file_path)}' is not UTF-8 encoded "
            f"(invalid byte at position {exc.start}). Power BI TMDL files are "
            f"UTF-8: reopen it and save it again with that encoding."
        ) from exc


# Writes are serialized per file. Without this, two tools editing the same
# table both read the starting lines and the last one to write silently
# erases the first one's change: `os.replace` guarantees the file is never
# truncated, not that nobody lost their work.
_file_locks: dict[str, threading.Lock] = {}
_file_locks_registry = threading.Lock()


def _lock_for_file(file_path: str) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(file_path))
    with _file_locks_registry:
        lock = _file_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _file_locks[key] = lock
        return lock


def file_fingerprint(file_path: str) -> tuple[int, int] | None:
    """
    (mtime_ns, size) of the file, or None if it doesn't exist.

    It is how a change between read and write is detected: the autodoc tools
    capture lines and block indices well before writing, and meanwhile Power
    BI Desktop, OneDrive or another call may have rewritten everything.
    Applying old line indices to a new file doesn't raise an error: it yields
    a plausibly corrupted file.
    """
    try:
        stat = os.stat(extended_path(file_path))
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


# Sentinel: `None` is a VALID fingerprint (it means "the file didn't exist"),
# so it cannot also mean "don't check".
NO_CHECK = object()


def write_tmdl_lines(file_path: str, lines: list[str], expected_fingerprint=NO_CHECK) -> None:
    """
    Single write path for TMDL files. `newline` translates the '\\n' of the
    in-memory lines into the file's original terminator, so a one-line edit
    stays a one-line edit on disk too.

    The write is atomic: first to a temporary file in the same folder, then
    `os.replace`. Opening the .tmdl directly in 'w' mode truncates it
    immediately, so an error halfway through (disk full, process killed, file
    locked by OneDrive sync) left a truncated table definition on disk: the
    model no longer opens in Power BI Desktop and the good version is already
    lost. With os.replace the existing file stays intact until the new one is
    complete.

    `expected_fingerprint` (from `file_fingerprint`, captured at read time)
    makes the write fail if the file changed in the meantime, instead of
    blindly overwriting someone else's changes.
    """
    folder = os.path.dirname(os.path.abspath(file_path))

    with _lock_for_file(file_path):
        if expected_fingerprint is not NO_CHECK:
            current = file_fingerprint(file_path)
            if current != expected_fingerprint:
                raise TmdlError(
                    f"'{os.path.basename(file_path)}' changed after it was read "
                    f"(modified by Power BI Desktop, by file sync or by another call). "
                    f"The change was NOT applied, so as not to overwrite someone "
                    f"else's: repeat the operation on the updated content."
                )

        eol = detect_eol(file_path)
        with_bom = _had_bom(file_path)
        os.makedirs(extended_path(folder), exist_ok=True)

        # The temporary name no longer includes the final file name: on long
        # paths every extra character gets closer to the limit, and the file
        # name added nothing that can't be inferred from the folder.
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig" if with_bom else "utf-8", newline=eol,
            dir=extended_path(folder), prefix=".tmp_", suffix=".part", delete=False,
        )
        temporary = handle.name
        try:
            with handle:
                handle.writelines(lines)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, extended_path(file_path))
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
