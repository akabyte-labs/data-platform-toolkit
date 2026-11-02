"""
pytest configuration: shared fixtures, fresh caches for every test, and the
manual bridge scripts kept out of the collection.

`python -m pytest` runs both these isolated tests and the self-contained
suite (`self_test.py`, through `test_self_test_suite.py`).
"""

from __future__ import annotations

import json
import os

import pytest

from mcp_data_platform_toolkit.mcp_common import user_folder
from mcp_data_platform_toolkit.semantic_model import description_rules, model_loader, project_paths
from mcp_data_platform_toolkit.sql_project import project as sql_project

# Manual checks against the official Microsoft executable, not tests: they
# need it installed and a real project (see docs/development.md, "Testing").
collect_ignore = ["test_bridge_write.py", "test_bridge_connection.py"]


@pytest.fixture(autouse=True)
def private_user_folder(tmp_path_factory, monkeypatch):
    """
    No test reads or writes the real folder of whoever runs them (their
    rules for the descriptions, their lineage configurations): the settings
    folder (APPDATA, XDG_CONFIG_HOME) and Documents point to a temporary
    folder, and no folder is chosen yet.
    """
    base = tmp_path_factory.mktemp("user")
    monkeypatch.setenv("APPDATA", str(base / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / ".config"))
    monkeypatch.delenv(user_folder.ENV_FOLDER, raising=False)
    documents = base / "Documents" / user_folder.APP_FOLDER_NAME
    monkeypatch.setattr(user_folder, "documents_folder", lambda: documents)
    return base


@pytest.fixture(autouse=True)
def guidelines_already_asked(monkeypatch):
    """
    The user counts as already asked for their own rules, so the writes of
    the tests don't stop at the question. Returns the real check, for the
    tests of the question itself.
    """
    real = description_rules.needs_guidelines_question
    monkeypatch.setattr(description_rules, "needs_guidelines_question", lambda: False)
    return real


@pytest.fixture(autouse=True)
def fresh_caches():
    """No test sees the model, the paths or the SQL lineage cached by another one."""
    model_loader.clear_model_cache()
    project_paths.clear_path_cache()
    sql_project.clear_sql_cache()
    yield
    model_loader.clear_model_cache()
    project_paths.clear_path_cache()
    sql_project.clear_sql_cache()


@pytest.fixture
def write_files(tmp_path):
    """
    Writes {relative path: text or JSON object} under a base folder (default:
    the test's tmp_path) and returns the base. Text is written with CRLF, the
    line ending Power BI Desktop uses.
    """
    def write(files: dict, base: str | None = None) -> str:
        root = str(base or tmp_path)
        for relative, content in files.items():
            path = os.path.join(root, *relative.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if isinstance(content, str):
                with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
                    handle.write(content)
            else:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(content, handle)
        return root
    return write


@pytest.fixture
def demo_project(tmp_path):
    """The synthetic project of the self-contained suite: Sales, Customer, a calculation group."""
    from tests.self_test import create_pbip_project
    return create_pbip_project(str(tmp_path / "pbip"))
