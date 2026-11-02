"""
Debug script for the bridge to the official Microsoft server: opens and
closes two sessions in quick succession in the same process (like
list_official_powerbi_tools + call_official_powerbi_tool called one after
the other) and prints the full traceback instead of anyio's generic
TaskGroup message.

Usage:
    set TEST_PROJECT_PATH=C:\\path\\to\\Project.pbip
    python tests/test_bridge_connection.py

    or:  python tests/test_bridge_connection.py "C:\\path\\to\\Project.pbip"

It needs a real PBIP project and the official executable installed: the
bridge connects to the 'definition' folder on disk, so Power BI Desktop
doesn't need to be open. It is not part of the automated suite.
"""

import asyncio
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_data_platform_toolkit.powerbi_bridge.bridge import _open_session, CONNECT_TOOL_NAME, CONNECT_OPERATION
from mcp_data_platform_toolkit.semantic_model.project_paths import find_definition_folder


def project_path() -> str:
    """Project path from the command line or from TEST_PROJECT_PATH."""
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    from_env = os.environ.get("TEST_PROJECT_PATH", "").strip()
    if from_env:
        return from_env
    raise SystemExit(
        "Specify the PBIP project to use: TEST_PROJECT_PATH environment variable "
        "or first command-line argument.\n"
        '  example: python tests/test_bridge_connection.py "C:\\path\\to\\Project.pbip"'
    )


async def session_1_list_tools() -> None:
    print("--- Session 1: list_tools ---")
    async with _open_session() as session:
        result = await session.list_tools()
        print(f"   -> {len(result.tools)} tools found")


async def session_2_connect_and_call(project: str) -> None:
    print("--- Session 2: connect + call ---")
    # Same resolution used by the bridge: no manual os.path.join.
    definition = find_definition_folder(project)
    print(f"   definition: {definition}")

    async with _open_session() as session:
        connection = await session.call_tool(
            CONNECT_TOOL_NAME,
            {"request": {"operation": CONNECT_OPERATION, "folderPath": definition}},
        )
        print("   connect is_error:", getattr(connection, "is_error", None))

        result = await session.call_tool("model_operations", {"request": {"operation": "Get"}})
        print("   call is_error:", getattr(result, "is_error", None))


async def main() -> None:
    project = project_path()
    await session_1_list_tools()
    print("(the two sessions start one after the other, without pauses)")
    await session_2_connect_and_call(project)


if __name__ == "__main__":
    try:
        asyncio.run(main())
        print("\nOK: both sessions completed without errors.")
    except SystemExit:
        raise
    except BaseException:
        print("\n--- FULL TRACEBACK ---")
        traceback.print_exc()
        exception = sys.exc_info()[1]
        if hasattr(exception, "exceptions"):  # ExceptionGroup, Python 3.11+
            print("\n--- SUB-EXCEPTIONS ---")
            for index, sub in enumerate(exception.exceptions):
                print(f"[{index}] {type(sub).__name__}: {sub}")
                traceback.print_exception(type(sub), sub, sub.__traceback__)
