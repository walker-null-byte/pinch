"""Technique `context`: put a compact repo map and the most relevant files in the first prompt.

This saves the list-directory and read-file round trips a model would otherwise spend finding its way.
Files are ranked by matching the task text (and any traceback in it) against paths, top-level symbol names
and file contents. Only text files are read, never secrets, and never more than a few thousand characters.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".pinch", "dist", "build",
             ".mypy_cache", ".ruff_cache", ".tox", "site-packages", ".idea", ".vscode", "target"}
SECRET_PARTS = {".ssh", ".aws", ".gnupg", "secrets"}  # same spirit as coding-tools' blocklist
SECRET_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json", ".pinch-confidential"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".json", ".yaml", ".yml", ".cfg", ".ini", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
                 ".java", ".c", ".h", ".cpp", ".rb", ".sh", ".html", ".css", ".sql"}
MAX_MAP_FILES = 120
MAX_SYMBOLS = 8


def source_files(repo: Path) -> list[Path]:
    out = []
    for folder, dirs, names in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d not in SECRET_PARTS)
        for name in sorted(names):
            path = Path(folder) / name
            if name in SECRET_NAMES or path.suffix not in TEXT_SUFFIXES or path.is_symlink():
                continue
            out.append(path)
    return out


def python_symbols(text: str) -> list[str]:
    """Top-level functions and classes, e.g. ['add(a, b)', 'Stack[push, pop]']."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    symbols = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"{node.name}({', '.join(a.arg for a in node.args.args)})")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("__")]
            symbols.append(f"{node.name}[{', '.join(methods[:5])}]" if methods else node.name)
    return symbols[:MAX_SYMBOLS]


def repo_map(repo: Path, files: list[Path], texts: dict[Path, str]) -> str:
    lines = []
    for path in files[:MAX_MAP_FILES]:
        rel = path.relative_to(repo).as_posix()
        symbols = python_symbols(texts[path]) if path.suffix == ".py" else []
        lines.append(f"{rel}: {', '.join(symbols)}" if symbols else rel)
    if len(files) > MAX_MAP_FILES:
        lines.append(f"... and {len(files) - MAX_MAP_FILES} more files")
    return "\n".join(lines)


def words(text: str) -> set[str]:
    """Lowercase words of 3+ letters; snake_case and CamelCase are split."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return {w for w in re.findall(r"[a-zA-Z]{3,}", text.lower())}


def traceback_paths(task: str) -> list[str]:
    found = re.findall(r'File "([^"]+)", line \d+', task) + re.findall(r"([\w./-]+\.\w+):\d+", task)
    return [p for p in found if "/" in p or "." in p]


def score(repo: Path, path: Path, text: str, task: str, task_words: set[str], trace: list[str]) -> int:
    rel = path.relative_to(repo).as_posix()
    points = 0
    if rel in task or path.name in task:
        points += 10
    if any(rel.endswith(t.lstrip("./")) or t.endswith(rel) for t in trace):
        points += 20
    stem_words = words(path.stem) | words(path.parent.name)
    points += 4 * len(stem_words & task_words)
    for symbol in python_symbols(text):
        points += 3 * len(words(re.split(r"[(\[]", symbol)[0]) & task_words)
    points += min(len(task_words & words(text)), 5)
    return points


def build_context(repo: Path, task: str, max_files: int = 3, max_file_chars: int = 4000, max_total_chars: int = 9000
                  ) -> tuple[str, dict[str, str]]:
    """Returns (text to append to the first prompt, {relative path: full file text} of the files shown)."""
    files, texts = [], {}
    for path in source_files(repo):
        try:
            if path.stat().st_size > 200_000:
                continue
            texts[path] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files.append(path)
    if not files:
        return "", {}
    task_words, trace = words(task), traceback_paths(task)
    ranked = sorted(files, key=lambda p: (-score(repo, p, texts[p], task, task_words, trace), p.as_posix()))
    chosen, total = [], 0
    for path in ranked:
        if len(chosen) == max_files or score(repo, path, texts[path], task, task_words, trace) < 4:
            break
        size = min(len(texts[path]), max_file_chars)
        if total + size > max_total_chars:
            continue
        chosen.append(path)
        total += size

    parts = ["[pinch: repo map, files with their top-level symbols]", repo_map(repo, files, texts)]
    shown: dict[str, str] = {}
    if chosen:
        parts.append("[pinch: the files most relevant to this task, so you do not need to list or read them again]")
    for path in chosen:
        rel, text = path.relative_to(repo).as_posix(), texts[path]
        body = text if len(text) <= max_file_chars else text[:max_file_chars] + "\n[cut here; use read_file with offset for the rest]"
        parts.append(f"--- {rel} ---\n{body}")
        if len(text) <= max_file_chars:
            shown[rel] = text  # the model has the whole file
    return "\n\n".join(parts), shown
