"""
Entry point of Data Platform Toolkit: the `mcp-data-platform-toolkit`
command, also `python -m mcp_data_platform_toolkit`.

Access mode (default: read-only)
    mcp-data-platform-toolkit --access=full_access
    MCP_ACCESS_MODE=full_access mcp-data-platform-toolkit

Bridge to the official Microsoft server (default: auto)
    --msft-bridge=auto|on|off      or  MCP_MSFT_BRIDGE=auto|on|off
    'auto' registers the bridge only if the official executable is reachable.
    In read-only mode only `list_official_powerbi_tools` (and the result
    detail tool) are exposed.

Installation check (does not start the MCP protocol)
    mcp-data-platform-toolkit --check
    mcp-data-platform-toolkit --check "C:\\path\\to\\Project.pbip"

The user's own files (see rules_cli)
    mcp-data-platform-toolkit --guidelines [project]
    mcp-data-platform-toolkit --lineage-config <database project>
    mcp-data-platform-toolkit --user-folder [folder]
"""

import importlib.metadata
import json
import os
import sys

from mcp_data_platform_toolkit import __version__

DISTRIBUTION = "mcp-data-platform-toolkit"

# Version check BEFORE importing any project module: with an older Python the
# `str | None` and `dict[str, str]` syntax fails at import time with a
# SyntaxError, and the MCP client only shows "the server doesn't start".
# Better one line that says exactly what is missing.
if sys.version_info < (3, 10):
    sys.exit(
        f"ERROR: Python 3.10 or later is required, but this interpreter is "
        f"{sys.version.split()[0]} ({sys.executable}).\n"
        f"In the MCP client configuration the 'command' field must point to "
        f"the interpreter the dependencies were installed on."
    )

try:
    from mcp.server.mcpserver import MCPServer
except ImportError as _exc:  # pragma: no cover - startup diagnostics
    sys.exit(
        f"ERROR: missing or incompatible dependencies ({_exc}).\n"
        f"Reinstall the server with the interpreter set in 'command':\n"
        f'  "{sys.executable}" -m pip install --upgrade {DISTRIBUTION}'
    )

from mcp_data_platform_toolkit.lineage import lineage_tools
from mcp_data_platform_toolkit.powerbi_bridge import bridge as powerbi_modeling_mcp
from mcp_data_platform_toolkit.reports import report_tools
from mcp_data_platform_toolkit.semantic_model import reader_tools
from mcp_data_platform_toolkit.semantic_model import writer_tools
from mcp_data_platform_toolkit.mcp_common import user_folder
from mcp_data_platform_toolkit.semantic_model.project_paths import allowed_roots

SERVER_NAME = "Data Platform Toolkit"

# The name the client configuration gives the server.
CLIENT_KEY = "data-platform-toolkit"

# The Python uvx runs the server on: the newest one CI tests, the same as
# '.python-version' for 'uv run'. Without it uvx takes the newest Python on
# the machine, and on one too new for a dependency the installation fails
# (PyYAML had no package for Python 3.15 yet). uv downloads it if needed.
UVX_PYTHON = "3.13"

FULL_ACCESS_ALIASES = {"full_access", "full-access", "admin", "write", "rw"}
READ_ONLY_ALIASES = {"read_only", "read-only", "readonly", "reader", "ro"}


def _log(message: str) -> None:
    # stdout is reserved for the MCP stdio protocol: log to stderr.
    print(f"STATUS: {message}", file=sys.stderr)


def _parse_cli() -> tuple[bool, str]:
    """Reads the custom arguments and removes them from sys.argv (MCPServer doesn't know them)."""
    read_only = os.environ.get("MCP_ACCESS_MODE", "").lower() not in FULL_ACCESS_ALIASES
    bridge_mode = os.environ.get("MCP_MSFT_BRIDGE", "auto").lower()

    to_remove = []
    for arg in sys.argv[1:]:
        if arg.startswith("--access="):
            value = arg.split("=", 1)[1].lower()
            if value in FULL_ACCESS_ALIASES:
                read_only = False
            elif value in READ_ONLY_ALIASES:
                read_only = True
            else:
                _log(f"unrecognized --access value ('{value}'): staying read-only.")
            to_remove.append(arg)
        elif arg.startswith("--msft-bridge="):
            bridge_mode = arg.split("=", 1)[1].lower()
            to_remove.append(arg)

    for arg in to_remove:
        sys.argv.remove(arg)

    if bridge_mode not in ("auto", "on", "off"):
        _log(f"unrecognized --msft-bridge value ('{bridge_mode}'): using 'auto'.")
        bridge_mode = "auto"

    return read_only, bridge_mode


def run_check(project_path: str = "") -> int:
    """
    Installation check, meant for the first setup and for bug reports: prints
    to stdout everything needed to understand why the client doesn't
    connect, without reading the code.

    Invoked with `mcp-data-platform-toolkit --check [project_path]`; it does
    NOT start the MCP protocol, so it can be run by hand from a terminal.
    """
    lines = [
        "=== INSTALLATION CHECK ===",
        f"server version  : {__version__}",
        f"interpreter     : {sys.executable}",
        f"Python          : {sys.version.split()[0]}",
        f"package         : {os.path.dirname(os.path.abspath(__file__))}",
        "",
        "Configuration to paste into the MCP client (valid JSON, ready to use).",
    ]
    # The suggested configurations are READ-ONLY. Suggesting a hard-coded
    # '--access=full_access' would cancel the server's safe default through a
    # copy-pasted line, and the very first contact with the tool would already
    # have write permissions on the model.
    #
    # Two of them: with uvx, which installs the server by itself but keeps
    # the version in its cache (a new release needs '@latest'), and with the
    # interpreter running this check. Under uvx that interpreter lives in
    # uv's cache and can disappear, so neither one alone is right for
    # everybody.
    for title, command, args in (
            (f"With uvx (installs the server by itself; update it with {DISTRIBUTION}@latest):",
             "uvx", ["--python", UVX_PYTHON, DISTRIBUTION, "--access=read_only"]),
            ("Or with this Python environment, where the server is installed now:",
             sys.executable, ["-m", "mcp_data_platform_toolkit", "--access=read_only"])):
        configuration = {"mcpServers": {CLIENT_KEY: {"command": command, "args": args}}}
        lines.append(f"  {title}")
        lines.extend("    " + line for line in json.dumps(configuration, indent=2).splitlines())
    lines.append("")
    lines.append("  This configuration does NOT allow changes to the project files.")
    lines.append("  To enable the write tools replace '--access=read_only' with")
    lines.append("  '--access=full_access', after making sure the project is under")
    lines.append("  Git version control or that a backup copy exists.")
    lines.append("")
    lines.append("Dependencies:")

    problems = 0
    for module, distribution, purpose in (
            ("mcp", "mcp", "MCP protocol"),
            ("sqlglot", "sqlglot", "database project lineage (T-SQL parser)"),
            ("yaml", "PyYAML", "database project lineage (lineage.yaml)")):
        try:
            __import__(module)
        except ImportError:
            problems += 1
            lines.append(f"  [MISSING] {distribution:9s} -> needed for: {purpose}")
            continue
        # The installed distribution's version: the 'mcp' package has no
        # __version__, and "present" is useless in a bug report.
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = "installed (version unknown)"
        lines.append(f"  [ok] {distribution:9s} {version}")

    lines.append("")
    roots = allowed_roots()
    lines.append("Folders the server may access (MCP_ALLOWED_ROOTS): "
                 + (", ".join(roots) if roots else "any (not set)"))
    folder, source = user_folder.configured_folder()
    lines.append(f"Your folder (rules for the descriptions, lineage configurations): "
                 f"{folder or user_folder.default_folder()} ({source}; open the rules with "
                 f"--guidelines, move the folder with --user-folder)")

    lines.append("")
    lines.append("Microsoft bridge (optional):")
    if powerbi_modeling_mcp.is_bridge_available():
        lines.append(f"  [ok] {powerbi_modeling_mcp._discover_msft_exe_path()}")
    else:
        lines.append("  [absent] official server not found: the advanced editing features "
                     "(creating relationships, RLS, hierarchies...) will not be available.")
        lines.append("           This is not an error: everything else works.")

    if project_path:
        lines.append("")
        lines.append(f"Given project: {project_path}")
        try:
            from mcp_data_platform_toolkit.semantic_model.project_paths import (
                find_semantic_model_folder, list_table_files,
            )
            folder = find_semantic_model_folder(project_path)
            tables = list_table_files(project_path)
            lines.append(f"  [ok] semantic model : {folder}")
            lines.append(f"  [ok] tables found   : {len(tables)}")
        except Exception as exc:  # noqa: BLE001 - it is a diagnostic
            problems += 1
            lines.append(f"  [ERROR] {type(exc).__name__}: {exc}")

    lines.append("")
    lines.append("RESULT: ready to use." if not problems
                 else f"RESULT: {problems} problem(s) to fix (see above).")
    print("\n".join(lines))
    return 1 if problems else 0


# What the client's assistant is told when it connects. Short on purpose: it is read at every
# start. It answers what went wrong when the tools were right and the answer was not: a lineage
# rebuilt by hand from the SQL, "all the reports" said of a scan that skipped some, a description
# that states what no file says.
SERVER_INSTRUCTIONS = """\
Data Platform Toolkit: Power BI projects (PBIP, TMDL, PBIR) and the SQL Server database project that feeds them.

- Answer from what the tools return. Do not rebuild a lineage by reading SQL or TMDL files yourself: where the tools stop (at a file, at a layer), say where, and what describes the missing step (a rule in lineage.yaml, see test_lineage_config and suggest_lineage_rules).
- Always say what the tools report they did NOT cover: the 'Coverage' line of the end-to-end lineage, reports 'NOT analyzed' (PBIR-Legacy), 'to verify' matches, unresolved references, a response that starts with '[!] INCOMPLETE RESPONSE'. Never write 'all the reports' or 'not used' without that qualification.
- A lineage marked PROVISIONAL uses a structure inferred from the SQL that the user has not saved yet: say so.
- Descriptions: call get_description_guidelines first. Write only what the DAX, the lineage, the existing description or the user supports; ask for the business rules, agreements and meanings you cannot see, and list what you inferred.
- Write formulas as plain text.
"""


def build_server() -> MCPServer:
    read_only, bridge_mode = _parse_cli()
    mcp = MCPServer(SERVER_NAME, version=__version__, instructions=SERVER_INSTRUCTIONS)
    _log(f"version {__version__} on Python {sys.version.split()[0]}.")

    # Read and analysis tools: always available.
    reader_tools.register_reader_tools(mcp)
    report_tools.register_report_tools(mcp)
    lineage_tools.register_lineage_tools(mcp)

    if read_only:
        _log("started in READ-ONLY mode.")
    else:
        _log("started in FULL ACCESS mode.")
        writer_tools.register_writer_tools(mcp)
        lineage_tools.register_lineage_writer_tools(mcp)

    # Bridge to the official Microsoft server. The catalog is read-only and
    # stays available in read-only mode too.
    if bridge_mode == "off":
        _log("Microsoft bridge disabled.")
    elif bridge_mode == "on" or powerbi_modeling_mcp.is_bridge_available():
        powerbi_modeling_mcp.register_powerbi_modeling_tools(mcp, read_only=read_only)
        _log("Microsoft bridge registered" + (" (catalog only)." if read_only else "."))
    else:
        # The catalog is registered anyway: it is the only way the
        # explanation ("the official server is missing, this is what you
        # can't do and how to install it") reaches whoever is chatting. This
        # stderr message alone is hidden by MCP clients.
        powerbi_modeling_mcp.register_powerbi_modeling_tools(
            mcp, read_only=read_only, available=False)
        _log("WARNING: official Microsoft server not found. Not available: "
             f"{powerbi_modeling_mcp.OFFICIAL_ONLY_CAPABILITIES}.")
        _log(f"         Install the VS Code extension "
             f"'{powerbi_modeling_mcp.VSCODE_EXTENSION}' or set "
             f"POWERBI_MODELING_MCP_PATH, then restart the server.")
        _log("         Everything else in the server works normally.")

    return mcp


def main() -> None:
    """
    The `mcp-data-platform-toolkit` command: the MCP server on stdio, or one
    of the terminal commands (`--check`, `--guidelines`, `--lineage-config`,
    `--user-folder`).
    """
    others = [a for a in sys.argv[1:] if not a.startswith("--")]
    argument = others[0] if others else ""
    if "--user-folder" in sys.argv:
        from mcp_data_platform_toolkit.rules_cli import run_user_folder
        sys.exit(run_user_folder(argument))
    if "--lineage-config" in sys.argv:
        from mcp_data_platform_toolkit.rules_cli import run_lineage_config
        sys.exit(run_lineage_config(argument))
    if "--guidelines" in sys.argv:
        from mcp_data_platform_toolkit.rules_cli import run_guidelines
        sys.exit(run_guidelines(argument))
    if "--check" in sys.argv:
        sys.exit(run_check(argument))
    build_server().run()


if __name__ == "__main__":
    main()
