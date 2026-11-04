# Contributing

Thanks for your interest in improving **Data Platform Toolkit**.

## Reporting bugs and requesting features

Open an issue and include:

- what you did, what you expected, and what happened instead;
- the output of `mcp-data-platform-toolkit --check` (server version, Python, dependencies) and your MCP client;
- a **minimal, anonymized** TMDL/PBIR snippet that reproduces the problem.

Never attach real customer models, credentials or connection strings.

## Development setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"      # the package, editable, with pytest and ruff
python -m pytest
ruff check .
```

With [uv](https://docs.astral.sh/uv/) there is no environment to create by hand: `uv run --extra dev pytest` runs the tests, and `uv run mcp-data-platform-toolkit --check` the server, on the Python of `.python-version` (the one CI tests).

To try your changes in an MCP client, point it to the clone's environment: `command` is `.venv\Scripts\python.exe` (or `.venv/bin/python`) and `args` are `["-m", "mcp_data_platform_toolkit"]`.

The code lives in the `mcp_data_platform_toolkit` package; imports are absolute (`from mcp_data_platform_toolkit.semantic_model import ...`). A file the server reads at run time (like the two guides) goes inside the package, and `.github/scripts/check_dist.py` checks that the wheel carries it.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Keep changes focused: one fix or feature per pull request.
3. Follow the existing design:
   - reuse the existing modules instead of duplicating logic (see [Project structure](docs/development.md#project-structure)); in particular, write TMDL files only through `tmdl_io.write_tmdl_lines`, resolve names to paths through `model_loader.get_table_file_path` / `project_paths.simple_name`, and find the file that declares an element with `model_loader.locate_element` (a table can be split across files);
   - register tools through `mcp_common.errors.register_tools`, and signal failures by raising exceptions (they become `isError=True` responses);
   - write a detailed docstring for every tool: it is the description the model reads;
   - keep outputs compact and offer filters (e.g. `table_name`) rather than returning full dumps; bound them with `MAX_TOOL_OUTPUT_CHARS`, reducing detail rather than dropping elements;
   - write tools must only be registered in full access mode;
   - a folder the server writes to is never chosen by the language model alone: it comes from the project paths (confined, see `project_paths`), from a fixed list, or from the user (a terminal command, an elicitation form).
4. Add tests and make sure `python -m pytest` and `ruff check .` pass. New tests go in pytest files (`tests/test_*.py`), each building its own project with the fixtures of `tests/conftest.py`; `tests/self_test.py` is the older self-contained suite. CI runs everything on Windows and Linux with Python 3.10 and 3.13, so don't rely on the platform's line endings or on a specific Python version. If you change the bridge, also run `tests/test_bridge_write.py` with the official executable installed.
5. Update the documentation in `docs/` if you add or change a tool or an option (`docs/tools.md`, `docs/configuration.md`; the README only when its first screen changes), and add an entry to `CHANGELOG.md` under "Unreleased". The README is also the description on PyPI, so its links and images stay absolute (`https://github.com/akabyte-labs/data-platform-toolkit/blob/main/...`); the pages in `docs/` use relative links.
6. Code, comments and messages are in English.

## Releasing

1. In `CHANGELOG.md`, rename "Unreleased" to the new version, and set the same version in `mcp_data_platform_toolkit/__init__.py`.
2. Merge to `main` with CI green.
3. Tag the merge commit `v<version>` (e.g. `v1.5.0`) and push the tag: `.github/workflows/publish.yml` checks that the tag is the package version, builds the archives and publishes them to PyPI.
4. Create the GitHub release from the tag, with the changelog entry as its text.

Publishing uses PyPI's trusted publishing, so no token is stored in the repository. It needs a one-time setup: on PyPI, a trusted publisher for the project `mcp-data-platform-toolkit` with this repository, the workflow `publish.yml` and the environment `pypi`; on GitHub, the environment `pypi` (Settings → Environments).

By contributing, you agree that your contributions are licensed under the MIT License.
