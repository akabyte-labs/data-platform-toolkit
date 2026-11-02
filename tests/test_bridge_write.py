"""
Manual check of the bridge in WRITE mode, on a throwaway copy.

Exercises the full cycle the automated suite cannot cover (it needs the
official Microsoft executable installed):

    ConnectFolder -> <write operation> -> ExportToTmdlFolder

and measures the impact on the WHOLE 'definition' folder, which is where the
bridge can do damage: the export re-serializes the whole model, not just the
modified file.

Usage:
    python tests/test_bridge_write.py

It touches no real project: it builds a synthetic model in a temporary
folder and deletes it at the end. If the official executable is not found
the check is skipped.

What this check has already found:
  - the bridge didn't check the outcome of ConnectFolder, so a folder that
    couldn't be deserialized produced the misleading message "No
    connectionName provided" instead of the real diagnosis;
  - errors raised inside the anyio session reached the caller as "unhandled
    errors in a TaskGroup", without the message;
  - the atomic write failed on paths close to the MAX_PATH limit.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_data_platform_toolkit.powerbi_bridge import bridge
from mcp_data_platform_toolkit.semantic_model.project_paths import find_definition_folder
from self_test import create_pbip_project


def complete_fixture(project: str) -> None:
    """
    The suite's synthetic project is not a deserializable TMDL folder:
    'database.tmdl' and the 'ref table' entries in model.tmdl are missing.
    """
    definition = os.path.join(project, "Demo.SemanticModel", "definition")
    tables = [os.path.splitext(n)[0]
              for n in sorted(os.listdir(os.path.join(definition, "tables")))]

    with open(os.path.join(definition, "database.tmdl"), "w", encoding="utf-8") as handle:
        handle.write("database Demo\n\tcompatibilityLevel: 1604\n")

    with open(os.path.join(definition, "model.tmdl"), "w", encoding="utf-8") as handle:
        handle.write("model Model\n\tculture: en-US\n"
                     "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n\n")
        for table in tables:
            handle.write(f"ref table '{table}'\n")


def folder_fingerprint(folder: str) -> dict[str, tuple[int, str]]:
    state: dict[str, tuple[int, str]] = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            with open(path, "rb") as handle:
                data = handle.read()
            state[os.path.relpath(path, folder)] = (
                len(data), hashlib.sha256(data).hexdigest())
    return state


async def run(project: str) -> int:
    from mcp_data_platform_toolkit.semantic_model import writer_tools

    create_pbip_project(project)
    complete_fixture(project)

    # Documentation written by the NATIVE tools: checks that the official
    # server's TOM round-trip doesn't drop it.
    writer_tools.write_smart_description(
        project, "Sales", "Total Sales",
        base_description="Total revenue for the selected period.", mode="overwrite")

    definition = find_definition_folder(project)
    before = folder_fingerprint(definition)
    print(f"definition folder : {definition}")
    print(f"files present     : {len(before)}")
    print()

    print("--- measure_operations / Create ---")
    response = await bridge.call_official_powerbi_tool(project, "measure_operations", {
        "request": {"operation": "Create", "definitions": [{
            "tableName": "Customer", "name": "Customer Count",
            "expression": "COUNTROWS(Customer)"}]},
    })
    print(response[:400])
    print()

    after = folder_fingerprint(definition)
    modified = sorted(k for k in before if k in after and before[k] != after[k])
    added = sorted(k for k in after if k not in before)
    removed = sorted(k for k in before if k not in after)

    print("--- impact on the whole 'definition' folder ---")
    print(f"modified: {len(modified)} of {len(before)} | "
          f"added: {len(added)} | removed: {len(removed)}")
    for name in modified:
        print(f"   ~ {name}  ({before[name][0]} -> {after[name][0]} bytes)")
    print()
    print("EXPECTED: the export re-serializes ALL files, not just the modified one.")
    print("          That is why the bridge should be used on a version-controlled project.")
    print()

    sales_path = os.path.join(definition, "tables", "Sales.tmdl")
    with open(sales_path, "r", encoding="utf-8-sig") as handle:
        sales_text = handle.read()
    customer_path = os.path.join(definition, "tables", "Customer.tmdl")
    with open(customer_path, "r", encoding="utf-8-sig") as handle:
        customer_text = handle.read()

    outcomes = [
        ("the measure created by the bridge is on disk", "Customer Count" in customer_text),
        ("the native tools' '///' description survives the round-trip",
         "Total revenue" in sales_text),
        ("the table's '///' comment survives", "Sales facts" in sales_text),
        ("no file was lost or added", not added and not removed),
    ]
    print("--- outcomes ---")
    problems = 0
    for description, outcome in outcomes:
        print(f"  {'ok  ' if outcome else 'FAIL'} {description}")
        problems += 0 if outcome else 1
    return problems


def main() -> int:
    if not bridge.is_bridge_available():
        print("Official executable not found: check skipped.")
        print("Set POWERBI_MODELING_MCP_PATH or install the VS Code extension.")
        return 0

    root = tempfile.mkdtemp(prefix="mcp_bridge_")
    project = os.path.join(root, "pbip")
    try:
        problems = asyncio.run(run(project))
    except Exception as exc:  # noqa: BLE001 - it is a manual check
        print(f"\nEXCEPTION {type(exc).__name__}: {exc}")
        return 2
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\nRESULT: {'ok' if not problems else str(problems) + ' problem(s)'}.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
