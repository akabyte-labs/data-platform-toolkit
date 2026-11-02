"""
Checks the built archives in a folder (default: dist): the wheel carries the
guides the server reads at run time, the source archive carries the tests
and none of the files that are not part of the server.

    python .github/scripts/check_dist.py dist
"""

from __future__ import annotations

import glob
import os
import sys
import tarfile
import zipfile

PACKAGE = "mcp_data_platform_toolkit"
# Read at run time: a wheel without them answers "guide not available".
WHEEL_REQUIRED = (
    f"{PACKAGE}/lineage/lineage_config_guide.md",
    f"{PACKAGE}/semantic_model/description_guidelines.md",
    f"{PACKAGE}/__main__.py",
)
SDIST_REQUIRED = ("tests/self_test.py", "pyproject.toml", "LICENSE", "README.md")
# Nothing outside the server: planning notes, CI, local folders.
SDIST_FORBIDDEN = ("Claude outputs", ".github", "sandbox")


def _only(folder: str, pattern: str) -> str:
    found = glob.glob(os.path.join(folder, pattern))
    if len(found) != 1:
        sys.exit(f"Expected one {pattern} in {folder}, found {found}")
    return found[0]


def main(folder: str) -> None:
    problems = []
    with zipfile.ZipFile(_only(folder, "*.whl")) as wheel:
        names = set(wheel.namelist())
    problems += [f"wheel: missing {name}" for name in WHEEL_REQUIRED if name not in names]
    problems += [f"wheel: unexpected {name}" for name in names
                 if not name.startswith((f"{PACKAGE}/", "mcp_data_platform_toolkit-"))]

    with tarfile.open(_only(folder, "*.tar.gz")) as sdist:
        names = {name.split("/", 1)[1] for name in sdist.getnames() if "/" in name}
    problems += [f"sdist: missing {name}" for name in SDIST_REQUIRED if name not in names]
    problems += [f"sdist: unexpected {name}" for name in sorted(names)
                 if name.startswith(SDIST_FORBIDDEN)]

    if problems:
        sys.exit("\n".join(problems))
    print("The wheel and the source archive carry what they should.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dist")
