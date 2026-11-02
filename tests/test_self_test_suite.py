"""The self-contained suite (`tests/self_test.py`), run by pytest as one test."""

from __future__ import annotations

from tests import self_test


def test_self_test_suite(capsys):
    code = self_test.main()
    output = capsys.readouterr().out
    failures = [line for line in output.splitlines() if line.startswith(("  FAIL", "  - "))]
    assert code == 0, "self_test.py reported failures:\n" + "\n".join(failures or output.splitlines()[-30:])
