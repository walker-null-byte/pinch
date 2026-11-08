"""Technique `stopongreen`: after an approved edit, pinch proposes running the repo's tests itself.

The proposal goes through the same policy check and approval gate as any other shell command. If the tests
pass AFTER having failed earlier in the session (a red-to-green change), pinch stops without another model call. Tests that pass and
never failed do not stop the run: the model decides when it is done.
"""

from __future__ import annotations

import os
from pathlib import Path

EDIT_TOOLS = {"edit_file", "write_file", "apply_patch", "search_and_replace"}
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pinch"}


def tests_present(repo: Path) -> bool:
    """Does this repo look like it has tests we can run? (coding-tools' run_test finds the command.)"""
    if any((repo / name).exists() for name in ("pytest.ini", "tox.ini", "package.json", "Cargo.toml", "Makefile")):
        return True
    for folder, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        if any(n.startswith("test_") and n.endswith(".py") or n.endswith("_test.py") for n in names):
            return True
    return False
