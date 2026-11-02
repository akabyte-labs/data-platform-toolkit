"""
Fallback integration with the official Power BI Modeling MCP Server
(microsoft/powerbi-modeling-mcp), used ONLY for local editing of PBIP/TMDL
projects on disk. It NEVER establishes a live connection to Power BI
Desktop or to a Fabric workspace -> no OAuth/Entra ID involved in this
module.

Guiding principle: this server's native tools (reader_tools/writer_tools)
take priority for the cases they already cover. This bridge exists for the
capabilities they don't cover (relationships, calculation groups,
partitions, security roles, hierarchies, perspectives, translations, bulk
TMDL import/export).

Limit of folder mode: since there is no Analysis Services engine behind it,
DAX queries, refresh and traces are not usable. On the other hand, the
project doesn't need to be open in Power BI Desktop.

Design notes:
  - the cloud-operation blocklist is applied to every 'operation' key found
    recursively in the arguments, not just arguments["request"]["operation"]
    (otherwise it could be bypassed);
  - blocked tool names are compared case-insensitively;
  - every call to the official server has a timeout, otherwise a hung
    subprocess would hang the calling MCP server too;
  - oversized output is summarized, not truncated: long lists become a
    per-field statistical profile (count, distinct values or min/max, plus a
    small sample of original elements) instead of a positional truncation
    that arbitrarily discards the tail of the data.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager

from mcp import Client, StdioServerParameters

from mcp_data_platform_toolkit.mcp_common import output_shaping
from mcp_data_platform_toolkit.mcp_common.errors import register_tools
from mcp_data_platform_toolkit.semantic_model.project_paths import (
    PathNotAllowedError,
    confined_path,
    find_definition_folder,
)

# ---------------------------------------------------------------------------
# Executable discovery
# ---------------------------------------------------------------------------

_EXE_NAME = "powerbi-modeling-mcp.exe"
_discovered_exe_path: str | None = None


def _discover_msft_exe_path() -> str:
    """
    Path to the official executable, in priority order:
      1. POWERBI_MODELING_MCP_PATH (explicit override);
      2. system PATH (npm install -g installation);
      3. VS Code extensions folder (VSIX/Marketplace installation).
    """
    global _discovered_exe_path
    if _discovered_exe_path is not None:
        return _discovered_exe_path

    env_path = os.environ.get("POWERBI_MODELING_MCP_PATH")
    if env_path and os.path.isfile(env_path):
        _discovered_exe_path = env_path
        return _discovered_exe_path

    which_path = shutil.which("powerbi-modeling-mcp") or shutil.which(_EXE_NAME)
    if which_path:
        _discovered_exe_path = which_path
        return _discovered_exe_path

    vscode_extensions = os.path.join(os.environ.get("USERPROFILE", ""), ".vscode", "extensions")
    candidate_patterns = [
        os.path.join(vscode_extensions, "analysis-services.powerbi-modeling-mcp-*", "server", _EXE_NAME),
        os.path.join(vscode_extensions, "analysis-services.powerbi-modeling-mcp-*", "extension", "server", _EXE_NAME),
    ]
    matches: list[str] = []
    for pattern in candidate_patterns:
        matches.extend(glob.glob(pattern))
    matches = sorted(set(matches), reverse=True)
    if matches:
        _discovered_exe_path = matches[0]
        return _discovered_exe_path

    raise FileNotFoundError(
        f"Could not find '{_EXE_NAME}'. Install the official server or set the "
        f"POWERBI_MODELING_MCP_PATH environment variable to its full path."
    )


def is_bridge_available() -> bool:
    """True if the official executable is reachable (raises no exception)."""
    try:
        _discover_msft_exe_path()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Connection configuration and blocklist
# ---------------------------------------------------------------------------

CONNECT_TOOL_NAME = "connection_operations"
CONNECT_OPERATION = "ConnectFolder"

# Tool fully hidden: connection management is internal to the bridge.
BLOCKED_TOOL_NAMES = {"connection_operations"}

# Operations blocked wherever they appear in the arguments: they require
# cloud/Entra ID authentication or change the active connection.
BLOCKED_OPERATIONS = {
    "deploytofabric", "connectfabric", "connectfolder",
    "connect", "disconnect", "clearlastused", "setlastused",
}

# Saving to disk. In ConnectFolder mode the official server DESERIALIZES the
# TMDL into an in-memory model: every change stays there and is lost when the
# subprocess closes, even if the response is {"success": true}. The only way
# to bring it back to the files is 'ExportToTmdlFolder' in the SAME session
# (for offline connections it reuses ConnectFolder's folderPath, so it doesn't
# need to be passed).
SAVE_TOOL_NAME = "database_operations"
SAVE_OPERATION = "ExportToTmdlFolder"

# Operations that do NOT modify the model: after these, saving is skipped.
# List derived from the official tools' schemas (v0.4.0). An operation not
# listed here is treated as a write: when in doubt the model is saved,
# because losing a change is worse than a useless re-serialization.
READ_ONLY_OPERATIONS = {
    "help", "find", "validate", "execute", "report",
    "get", "getcolumngroups", "getcolumns", "getdetailsbylcid",
    "getdetailsbyname", "geteffectivepermissions", "getgroup", "gethierarchies",
    "getitems", "getlastused", "getmeasures", "getpermissions", "getschema",
    "getstats", "getstatus", "gettables", "getvaliddetails", "getvalidnames",
    "list", "listactive", "listcolumngroups", "listcolumns",
    "listgroups", "listhierarchies", "listitems", "listlocalinstances",
    "listmeasures", "listpermissions", "listtables",
    "exportjson", "exporttmdl", "exporttmsl",
    # Already a save: must not be doubled.
    "exporttotmdlfolder", "importfromtmdlfolder",
}
# 'getconnection', 'listconnections' and 'renameconnection' used to be listed
# here too, but FORBIDDEN_OPERATION_FRAGMENTS ('connect') blocks them before
# this list is ever read: a read-only entry that can never be reached only
# hides which list is actually in charge. A check in the test suite keeps the
# two lists from contradicting each other again.

# ONLY these official tools are exposed (names verified against the catalog
# of powerbi-modeling-mcp 0.4.0). The set used to be empty, i.e. "everything
# is allowed except the blocklist above": an update of the Microsoft server
# introducing a new tool or a new cloud operation made it reachable
# automatically, and the executable isn't even pinned (it is discovered from
# PATH or the VS Code extensions, whatever version it is).
#
# Deliberately left OUT:
#   connection_operations   the connection is internal to the bridge (already blocked)
#   dax_query_operations    in folder mode there is no AS engine behind it
#   trace_operations        same: traces and refresh are not usable
#   transaction_operations  every call opens and closes its own session, so a
#                           transaction could not span them
ALLOWED_TOOL_NAMES: set[str] = {
    "database_operations",
    "model_operations",
    "table_operations",
    "column_operations",
    "measure_operations",
    "relationship_operations",
    "partition_operations",
    "calculation_group_operations",
    "user_hierarchy_operations",
    "security_role_operations",
    "perspective_operations",
    "culture_operations",
    "object_translation_operations",
    "named_expression_operations",
    "query_group_operations",
    "calendar_operations",
    "function_operations",
}

# Defense in depth on OPERATIONS: the exact blocklist above covers what exists
# today, these fragments cover what might arrive tomorrow. An operation that
# talks about cloud, deploy or authentication is outside the declared scope of
# this bridge (local editing on a PBIP folder only), whatever it is called.
FORBIDDEN_OPERATION_FRAGMENTS = (
    "fabric", "deploy", "publish", "workspace", "onelake", "powerbiservice",
    "signin", "signout", "login", "logout", "oauth", "token", "credential",
    "connect", "disconnect",
)

_tool_catalog_cache: list | None = None

# Version of the official server and expected tools missing from its catalog.
# The allow-list above is calibrated on a specific version: if the installed
# extension is a different version and the tool names changed, the catalog
# silently empties and the bridge seems "not to work" with nothing
# explaining why. These two values make the diagnosis immediate.
EXPECTED_OFFICIAL_VERSION = "0.4.0"
_server_version: str = ""
_missing_expected_tools: list[str] = []

# Serializes sessions: never two subprocesses of the official server running
# in parallel, with a grace period after each closes (cleanup isn't
# guaranteed synchronous, and starting a new one too soon causes an
# intermittent error in anyio's TaskGroup).
_session_lock = asyncio.Lock()
_SESSION_COOLDOWN_SECONDS = 0.5


def _number_from_env(name: str, default: float) -> float:
    """
    Positive number from an environment variable, falling back to the default.

    These used to be bare `float(...)` and `int(...)` at module level: a
    POWERBI_MODELING_MCP_TIMEOUT=abc made the module IMPORT fail, and with it
    the startup of the whole server, with a raw traceback: exactly the kind of
    failure the checks in `server.py` exist to prevent.
    """
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"STATUS: non-numeric value in {name} ('{raw}'): using {default}.", file=sys.stderr)
        return default
    if value <= 0:
        print(f"STATUS: non-positive value in {name} ('{raw}'): using {default}.", file=sys.stderr)
        return default
    return value


CALL_TIMEOUT_SECONDS = _number_from_env("POWERBI_MODELING_MCP_TIMEOUT", 120.0)
MAX_OUTPUT_CHARS = int(_number_from_env("POWERBI_MODELING_MCP_MAX_CHARS", 4000))


def _build_stdio_params() -> StdioServerParameters:
    exe_path = _discover_msft_exe_path()
    if exe_path.lower().endswith((".cmd", ".bat")):
        # Windows won't start .cmd/.bat as a native process: needs cmd.exe
        return StdioServerParameters(command="cmd", args=["/c", exe_path, "--start"], env={})
    return StdioServerParameters(command=exe_path, args=["--start"], env={})


@asynccontextmanager
async def _open_session():
    """
    Opens a full MCP session (subprocess + handshake) in the calling task and
    closes it within the same `async with`. No persistent session: anyio
    requires a cancel scope to be opened and closed in the same asyncio task.
    `read_timeout_seconds` also bounds the initial handshake, not just later calls.

    The cooldown runs after EVERY session, failed ones included: it used to be
    skipped when the session ended with an exception, exactly when the next
    one was most likely to start before the previous subprocess was gone.
    """
    global _server_version
    async with _session_lock:
        try:
            params = _build_stdio_params()
            async with Client(params, read_timeout_seconds=CALL_TIMEOUT_SECONDS) as client:
                _server_version = str(getattr(getattr(client, "server_info", None), "version", "") or "")
                yield client
        finally:
            await asyncio.sleep(_SESSION_COOLDOWN_SECONDS)


def _describe_exception(exc: BaseException) -> str:
    """
    The real cause of an exception, exception groups unwrapped. anyio reports
    a failure inside a session as "unhandled errors in a TaskGroup (1
    sub-exception)", a message that names no cause at all.
    """
    leaves: list[str] = []

    def walk(error: BaseException) -> None:
        children = getattr(error, "exceptions", None)
        if children:
            for child in children:
                walk(child)
            return
        text = str(error).strip()
        leaves.append(f"{type(error).__name__}: {text}" if text else type(error).__name__)

    walk(exc)
    return "; ".join(dict.fromkeys(leaves))


async def _timed(awaitable):
    """A call to the official server, bounded by the timeout."""
    return await asyncio.wait_for(awaitable, timeout=CALL_TIMEOUT_SECONDS)


_MAX_ARGUMENT_DEPTH = 30


def _collect_operations(node, _depth: int = 0) -> set[str]:
    """
    All operations declared in the arguments, at any depth.

    Strings containing JSON are inspected too: an argument passed as
    `{"request": "{\\"operation\\": \\"ConnectFabric\\"}"}` declared no
    operation for this filter, but could declare one to the official server.
    The depth limit keeps a deeply nested dictionary from exhausting the stack
    during the security check itself.
    """
    found: set[str] = set()
    if _depth > _MAX_ARGUMENT_DEPTH:
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() == "operation" and isinstance(value, str):
                found.add(value.strip().lower())
            found |= _collect_operations(value, _depth + 1)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_operations(item, _depth + 1)
    elif isinstance(node, str):
        text = node.strip()
        if text.startswith(("{", "[")) and "operation" in text.lower():
            try:
                found |= _collect_operations(json.loads(text), _depth + 1)
            except (ValueError, RecursionError):
                pass
    return found


# Argument keys whose value is a filesystem path ('folderPath',
# 'tmdlFolderPath', 'outputPath', 'directory'...).
_PATH_KEY_SUFFIXES = ("path", "folder", "directory", "dir")

# Keys ending in 'folder' that are MODEL properties, not filesystem paths:
# 'displayFolder' (measures, columns, hierarchies) and the 'folder' of a
# query group, both 'Sales\KPI'-style names. Matched by suffix, they made
# every create/update carrying a display folder fail as a "path outside the
# model". Their value is still checked when it looks like a filesystem path
# (absolute, or climbing with '..'): a display folder never does.
_DISPLAY_FOLDER_KEYS = {"displayfolder", "folder"}


def _looks_like_filesystem_path(value: str) -> bool:
    """Absolute on any platform ('/x', 'C:\\x', 'C:/x', '\\\\server\\share') or with a '..' segment."""
    text = value.strip()
    absolute = (text.startswith(("/", "\\\\"))
                or (len(text) >= 3 and text[0].isalpha() and text[1] == ":" and text[2] in "\\/"))
    return absolute or ".." in text.replace("\\", "/").split("/")


def _is_path_argument(key: str, value: str) -> bool:
    lowered = str(key).lower()
    if lowered in _DISPLAY_FOLDER_KEYS:
        return _looks_like_filesystem_path(value)
    return lowered.endswith(_PATH_KEY_SUFFIXES)


def _collect_path_arguments(node, _depth: int = 0) -> list[tuple[str, str]]:
    """(key, value) of every path-like argument, at any depth, JSON strings included."""
    found: list[tuple[str, str]] = []
    if _depth > _MAX_ARGUMENT_DEPTH:
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and _is_path_argument(key, value):
                found.append((str(key), value))
            found.extend(_collect_path_arguments(value, _depth + 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_path_arguments(item, _depth + 1))
    elif isinstance(node, str):
        text = node.strip()
        if text.startswith(("{", "[")):
            try:
                found.extend(_collect_path_arguments(json.loads(text), _depth + 1))
            except (ValueError, RecursionError):
                pass
    return found


def paths_outside_model(arguments, model_folder: str) -> list[str]:
    """
    Path arguments that don't point inside the project's semantic model.

    The bridge only checked the OPERATION names. 'ExportToTmdlFolder' and
    'ImportFromTmdlFolder' are allowed operations that take a folder: with an
    explicit path they could write the model anywhere on disk, or load a
    different one, contradicting the "project folder only" confinement of
    the rest of the server. Relative paths are refused too: they would be
    resolved against the official server's working directory, not the project.
    """
    outside = []
    for key, value in _collect_path_arguments(arguments):
        text = value.strip()
        if not text:
            continue
        try:
            if not os.path.isabs(text):
                raise PathNotAllowedError(text)
            confined_path(model_folder, text, f"'{key}'")
        except PathNotAllowedError:
            outside.append(f"{key}='{text}'")
    return outside


def forbidden_operations(arguments) -> set[str]:
    """Out-of-scope operations: those in the blocklist plus 'cloud' ones by pattern."""
    operations = _collect_operations(arguments)
    forbidden = operations & BLOCKED_OPERATIONS
    for operation in operations:
        if any(fragment in operation for fragment in FORBIDDEN_OPERATION_FRAGMENTS):
            forbidden.add(operation)
    return forbidden


def _looks_read_only(operation: str) -> bool:
    """
    An unknown operation with an unmistakable read prefix.

    It avoids rewriting the WHOLE `definition` folder for a read-only
    operation this list doesn't know yet (a new version of the official
    server): that export produces a huge Git diff for a no-op change, a full
    OneDrive resync and a concurrent-write window opened for no reason. All
    other unknown operations are still treated as writes: when in doubt, save.
    """
    return operation.startswith(("get", "list", "export", "find", "validate", "help"))


def _is_read_only_call(arguments) -> bool:
    """
    True only if ALL the operations declared in the arguments are reads.
    Arguments without an operation -> False (conservative: better a useless
    save than a lost change).
    """
    operations = _collect_operations(arguments)
    if not operations:
        return False
    return all(
        operation in READ_ONLY_OPERATIONS or _looks_read_only(operation)
        for operation in operations
    )


def _result_text(result) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def _failed(result) -> bool:
    """
    True if the call to the official server failed.

    `is_error` alone isn't enough: the official server answers application
    errors with a "successful" result containing
    `{"success":false,"message":...}`. Trusting `is_error` alone let a failed
    ConnectFolder pass as successful, and the bridge carried on as if the
    model were open.
    """
    if getattr(result, "is_error", False):
        return True
    text = _result_text(result)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        # Not JSON: the textual marker is all there is to go by.
        return '"success":false' in text.replace(" ", "")
    # JSON: the TOP-LEVEL outcome decides. Searching the text failed a whole
    # call because of a nested '"success": false' (one item of a batch, or a
    # string inside a DAX expression), and the writes that DID succeed were
    # then not saved to disk. Without a top-level outcome, the call failed
    # only if every item reports a failure: saving then would rewrite the
    # whole 'definition' folder for nothing.
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() == "success":
                return _is_false(value)
    outcomes = _nested_outcomes(data)
    return bool(outcomes) and not any(outcomes)


def _is_false(value) -> bool:
    return value is False or str(value).strip().lower() == "false"


def _nested_outcomes(node, _depth: int = 0) -> list[bool]:
    """The 'success' values of the items of a result, a few levels deep."""
    found: list[bool] = []
    if _depth > 4:
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() == "success" and not isinstance(value, (dict, list)):
                found.append(not _is_false(value))
            else:
                found.extend(_nested_outcomes(value, _depth + 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(_nested_outcomes(item, _depth + 1))
    return found


async def _get_filtered_catalog(client: Client) -> list:
    """Filtered catalog of official tools (in-process cache, metadata only)."""
    global _tool_catalog_cache, _missing_expected_tools
    if _tool_catalog_cache is not None:
        return _tool_catalog_cache

    result = await asyncio.wait_for(client.list_tools(), timeout=CALL_TIMEOUT_SECONDS)
    blocked = {name.lower() for name in BLOCKED_TOOL_NAMES}
    allowed = {name.lower() for name in ALLOWED_TOOL_NAMES}
    available = {tool.name.lower() for tool in result.tools}

    filtered = []
    for tool in result.tools:
        if tool.name.lower() in blocked:
            continue
        if allowed and tool.name.lower() not in allowed:
            continue
        filtered.append(tool)

    # An allow-listed tool that the server doesn't expose almost always means
    # a different version from the one the list was verified against.
    _missing_expected_tools = sorted(allowed - available)
    _tool_catalog_cache = filtered
    return filtered


# ---------------------------------------------------------------------------
# Output shaping
# ---------------------------------------------------------------------------
# The logic lives in output_shaping.py: progressive reduction (noise ->
# empty fields -> factoring -> profile) with the full payload kept in cache
# and retrievable via get_official_result_detail.


def shape_output(raw_text: str, max_chars: int = MAX_OUTPUT_CHARS, tool_name: str = "") -> str:
    """Adapts the official server's output to the token budget without losing the original data."""
    return output_shaping.shape_output(raw_text, max_chars=max_chars, tool_name=tool_name)


# ---------------------------------------------------------------------------
# MCP tools exposed to the model
# ---------------------------------------------------------------------------

# VS Code extension identifier, verified against the real installation path
# ('analysis-services.powerbi-modeling-mcp-<version>').
VSCODE_EXTENSION = "analysis-services.powerbi-modeling-mcp"

# What ONLY the official server can do. Reading relationships, OLS and the
# model is native: the list says "creating and editing" so the model isn't
# told that reading relationships needs the official server.
OFFICIAL_ONLY_CAPABILITIES = (
    "creating and editing relationships, security roles and RLS, hierarchies, perspectives, "
    "translations/cultures, partitions, Power Query parameters, bulk TMDL import/export"
)


def bridge_missing_message() -> str:
    """
    Explanation addressed to the MODEL, not to the logs.

    The startup warning ends up on stderr, where MCP clients hide it: whoever
    is chatting never sees it. And with no tool registered, the model doesn't
    even know those capabilities exist, and when asked to "create a
    relationship" it says it's not possible, without saying why or how to fix
    it. This text is the tool's response when the executable is missing: it
    is the only place where the information actually reaches the user.
    """
    return (
        "The official Microsoft server (Power BI Modeling MCP) is NOT installed "
        "on this machine, so the advanced features are not available.\n\n"
        f"Not available: {OFFICIAL_ONLY_CAPABILITIES}.\n\n"
        "Everything else in this server works: reading the model and its "
        "relationships, DAX lineage, "
        "report usages, automatic documentation, creating measures and "
        "calculation items, displayFolder, formatString, visibility and "
        "Object-Level Security.\n\n"
        "To enable the advanced features, either:\n"
        f"  - install the VS Code extension '{VSCODE_EXTENSION}';\n"
        "  - or set the POWERBI_MODELING_MCP_PATH environment variable to the "
        "full path of 'powerbi-modeling-mcp.exe'.\n"
        "In both cases the MCP server must be restarted to detect it.\n\n"
        "Report this to the user instead of looking for an alternative: the "
        "operations listed above cannot be achieved with the other tools."
    )


def _version_warning() -> str:
    """
    Warning when the installed official server doesn't expose the expected tools.

    The allow-list is verified against a specific version of the executable,
    which CANNOT be installed as a dependency of this package (it is a .NET executable
    distributed as a VS Code extension or via npm, not a Python package). The
    only honest way to handle the version gap is to notice it at runtime and
    say so.
    """
    if not _missing_expected_tools:
        return ""
    return (
        f"\nWARNING: the installed official server ({_server_version or 'unknown version'}) "
        f"doesn't expose {len(_missing_expected_tools)} tools expected by this bridge, verified "
        f"against version {EXPECTED_OFFICIAL_VERSION}: {', '.join(_missing_expected_tools)}. "
        f"Probably a different version of the 'powerbi-modeling-mcp' extension. "
        f"The corresponding features are not available; the rest of the server "
        f"works normally."
    )


async def list_official_powerbi_tools() -> str:
    """
    Lists the tools of the official Power BI Modeling MCP Server (Microsoft)
    available as a fallback, with name, description and argument schema.
    USE THIS TOOL before 'call_official_powerbi_tool'. For creating/formatting
    measures, setting display folders or visibility, or generating
    documentation, always prefer this server's native tools
    (get_description_guidelines, write_smart_description, set_display_folder,
    set_item_visibility, format_measure, create_measure,
    create_calculation_item): this list is for capabilities those tools don't
    cover (relationships, calculation groups, partitions, security roles,
    hierarchies, perspectives, translations/cultures, Power Query parameters,
    bulk TMDL import/export).
    NOT available: DAX queries, data refresh and traces, because PBIP folder
    mode has no Analysis Services engine behind it.

    If the official server is not installed, this tool does NOT fail: it
    explains what is missing, what keeps working and how to install it.
    """
    if not is_bridge_available():
        return bridge_missing_message()

    # Errors are recorded inside the session and raised after it is closed:
    # raised inside, anyio wraps them and the cause is lost (see
    # `call_official_powerbi_tool`).
    catalog = None
    error: str | None = None
    try:
        async with _open_session() as client:
            try:
                catalog = await _get_filtered_catalog(client)
            except Exception as exc:  # noqa: BLE001 - reported after the session
                error = f"The official server did not return its tool catalog: {_describe_exception(exc)}."
    except Exception as exc:  # noqa: BLE001 - subprocess start/handshake/teardown
        if catalog is None and error is None:
            error = f"Could not open a session with the official server: {_describe_exception(exc)}."
    if error is not None:
        raise RuntimeError(error)

    version_warning = _version_warning()

    if not catalog:
        return (
            "No official tools available: none of the tools expected by this "
            "bridge is exposed by the installed official server." + (version_warning or "")
        )

    lines = [f"=== OFFICIAL POWER BI TOOLS (fallback) === "
             f"official server: {_server_version or 'unknown version'}"]
    if version_warning:
        lines.append(version_warning.strip())
    for tool in catalog:
        schema = json.dumps(tool.input_schema, separators=(",", ":")) if tool.input_schema else "{}"
        lines.append(f"- {tool.name}: {tool.description}\n  schema: {schema}")
    return "\n".join(lines)


async def call_official_powerbi_tool(project_path: str, tool_name: str, arguments: dict) -> str:
    """
    Invokes a tool of the official Power BI Modeling MCP Server (Microsoft) on
    a local PBIP project, as a fallback for operations not covered by this
    server's native tools.

    Use ONLY after consulting 'list_official_powerbi_tools'. The connection
    is established exclusively in local-folder mode (ConnectFolder): it never
    connects to a live Power BI Desktop instance or a Fabric workspace, so no
    Entra ID authentication is required. The project must NOT be open in
    Power BI Desktop: the work happens on the TMDL files on disk.

    In folder mode the official server has no Analysis Services engine behind
    it, so DAX queries (dax_query_operations), data refresh and traces are NOT
    available: they answer 'not supported on offline connections'.

    Write operations are serialized to disk automatically at the end of the
    call: the export rewrites the WHOLE 'definition' folder, so work on a
    project under Git version control or on a backup copy.

    Args:
        project_path: Absolute path to the Power BI project root (.pbip file
            or project folder), the same one used by the other tools in this
            server.
        tool_name: Exact name of the official tool to invoke.
        arguments: Arguments in the form {"request": {"operation": ..., ...}},
            following the schema returned by list_official_powerbi_tools.
    """
    if tool_name.lower() in {name.lower() for name in BLOCKED_TOOL_NAMES}:
        raise ValueError(
            f"Tool '{tool_name}' is not exposed: connection management is internal to the bridge."
        )

    blocked_operations = forbidden_operations(arguments)
    if blocked_operations:
        raise ValueError(
            f"Operations not allowed through this bridge: {', '.join(sorted(blocked_operations))}. "
            f"They require cloud/Fabric authentication or change the active connection "
            f"(intended scope: local editing on a PBIP folder only)."
        )

    definition_folder = find_definition_folder(project_path)

    outside = paths_outside_model(arguments, os.path.dirname(definition_folder))
    if outside:
        raise ValueError(
            f"Paths not allowed through this bridge: {', '.join(outside)}. Paths must be "
            f"absolute and inside the project's semantic model "
            f"('{os.path.dirname(definition_folder)}'). Omit the path to use the project folder."
        )

    # Errors are NOT raised inside `async with _open_session()`: anyio catches
    # them in its TaskGroup and presents them to the caller as "unhandled
    # errors in a TaskGroup (1 sub-exception)", throwing the message away.
    # They are collected here and raised after the session is closed, so the
    # diagnosis reaches the model intact. That includes TIMEOUTS and transport
    # errors of each call: they used to escape from inside the session, and a
    # timeout while saving lost the change with an error that named no cause.
    error: str | None = None
    result = None
    saved = False

    try:
        async with _open_session() as client:
            try:
                catalog = await _get_filtered_catalog(client)
                if tool_name not in {t.name for t in catalog}:
                    error = (
                        f"Tool '{tool_name}' is not available or not allowed. "
                        f"Use list_official_powerbi_tools for the allowed list."
                    )
                else:
                    # The outcome of the connection must be CHECKED. Without
                    # this check, a failed ConnectFolder (folder that can't be
                    # deserialized: missing 'database.tmdl', compatibilityLevel
                    # too low, invalid TMDL) went unnoticed and the next call
                    # answered "No connectionName provided and no last used
                    # connection available": a message that sends you looking
                    # for a connection problem where the problem is the model.
                    connection = await _timed(client.call_tool(
                        CONNECT_TOOL_NAME,
                        {"request": {"operation": CONNECT_OPERATION, "folderPath": definition_folder}},
                    ))
                    if _failed(connection):
                        error = (
                            f"The official server could not open the model folder "
                            f"('{definition_folder}'), so NO operation was executed and "
                            f"nothing was modified. Detail: {_result_text(connection)[:500]}"
                        )
            except Exception as exc:  # noqa: BLE001 - reported after the session
                error = (
                    f"The official server could not be prepared ({_describe_exception(exc)}): "
                    f"NO operation was executed and nothing was modified."
                )

            if error is None:
                try:
                    result = await _timed(client.call_tool(tool_name, arguments))
                except Exception as exc:  # noqa: BLE001 - reported after the session
                    error = (
                        f"Tool '{tool_name}' did not complete ({_describe_exception(exc)}). Nothing "
                        f"was saved to disk: whatever the official server applied in memory was "
                        f"discarded with the session."
                    )

            # The model loaded by ConnectFolder lives in memory: without this
            # step the change would vanish when the session closes, despite
            # having answered {"success": true}. Skipped for reads only and
            # when the call failed (there is nothing to save).
            if error is None and not _is_read_only_call(arguments) and not _failed(result):
                try:
                    export = await _timed(
                        client.call_tool(SAVE_TOOL_NAME, {"request": {"operation": SAVE_OPERATION}}))
                except Exception as exc:  # noqa: BLE001 - reported after the session
                    error = (
                        f"Tool '{tool_name}' reported success but saving to disk ({SAVE_OPERATION}) "
                        f"did not complete ({_describe_exception(exc)}): the change may NOT have "
                        f"been written, or only partly. Check the project folder (e.g. with "
                        f"'git status') before repeating the operation."
                    )
                else:
                    if _failed(export):
                        error = (
                            f"Tool '{tool_name}' reported success but saving to disk "
                            f"({SAVE_OPERATION}) failed: the change was NOT written to the "
                            f"project and was lost. Detail: {_result_text(export)[:500]}"
                        )
                    else:
                        saved = True
    except Exception as exc:  # noqa: BLE001 - subprocess start/handshake/teardown
        # A failure while CLOSING a session that already produced its result
        # changes nothing about that result: only a session that never got
        # that far is an error.
        if error is None and result is None:
            error = f"Could not open a session with the official server: {_describe_exception(exc)}."

    if error is not None:
        raise RuntimeError(error)

    raw_text = _result_text(result)
    if saved:
        raw_text += "\n[bridge] change serialized to disk (ExportToTmdlFolder)."
    return shape_output(raw_text, tool_name=tool_name)


def get_official_result_detail(
    result_id: str,
    path: str = "",
    offset: int = 0,
    limit: int = 50,
    max_chars: int = MAX_OUTPUT_CHARS,
) -> str:
    """
    Retrieves a portion of the full result of a previous
    'call_official_powerbi_tool' call. When that response was reduced to fit
    the token budget, the original payload is still available here: USE THIS
    TOOL instead of re-running the call when you need a detail that was
    profiled away (for example the full list of an element's fields, or the
    expression of a specific measure).

    Args:
        result_id: Identifier reported in the note of the reduced response.
        path: Path to extract, e.g. 'tables[0].columns' or 'model.tables'.
            Empty for the root.
        offset: Index of the first element to return, if the path points to a list (>= 0).
        limit: Maximum number of elements returned (lists only, >= 1).
        max_chars: Character budget of the response, at most the server's
            output budget (MCP_MAX_OUTPUT_CHARS).
    """
    # Bounded: the budget used to be whatever the caller asked for (a whole
    # payload of megabytes in one response), and a negative offset sliced
    # from the end of the list while the header claimed "items -5-...".
    max_chars = min(max(int(max_chars), 200), output_shaping.MAX_TOOL_OUTPUT_CHARS)
    offset = max(int(offset), 0)
    limit = max(int(limit), 1)
    entry = output_shaping.get_result(result_id)
    portion = output_shaping.resolve_path(entry["data"], path)

    header = f"[result {result_id} | tool '{entry['tool']}' | path '{path or '(root)'}'"
    if isinstance(portion, list):
        total = len(portion)
        portion = portion[offset: offset + limit]
        header += f" | items {offset}-{offset + len(portion)} of {total}"
    header += "]"

    return header + "\n" + output_shaping.shape_payload(portion, max_chars, result_id)


def register_powerbi_modeling_tools(mcp, read_only: bool = True, available: bool = True) -> None:
    """
    Registers the fallback tools toward the official Microsoft server.

    In read-only mode only the catalog is exposed: `call_official_powerbi_tool`
    can modify the model and is reserved for full-access mode.
    The detail-retrieval tool only reads from the in-process cache, so it is
    always available.

    With `available=False` (executable not found) ONLY the catalog is
    registered, and it answers by explaining what is missing. Previously
    nothing was registered in that case: the model didn't know those
    capabilities existed and answered "it's not possible" without saying why
    or how to fix it.
    """
    register_tools(mcp, {"list_official_powerbi_tools": list_official_powerbi_tools})
    if not available:
        return

    register_tools(mcp, {"get_official_result_detail": get_official_result_detail})
    if not read_only:
        register_tools(mcp, {"call_official_powerbi_tool": call_official_powerbi_tool})
