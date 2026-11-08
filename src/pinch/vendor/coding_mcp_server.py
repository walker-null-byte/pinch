"""Coding MCP Server — gives Claude Desktop (or any MCP client)
all the tools needed to read, write, search, edit, test, and manage code.

Requires turbomcp>=0.3.0 (Streamable HTTP + stdio, JSON-RPC 2.0).

Run with:
    turbomcp serve coding_mcp_server.py              # stdio (Claude Desktop local)
    turbomcp serve coding_mcp_server.py --http --port 8000  # Streamable HTTP (remote)
    turbomcp serve coding_mcp_server.py --http --no-auth   # localhost only

Or directly:
    python coding_mcp_server.py                        # stdio
    python coding_mcp_server.py --http --port 8000     # http

Claude Desktop local (claude_desktop_config.json):
    {
      "mcpServers": {
        "coding-tools": {
          "command": "turbomcp",
          "args": ["serve", "/ABSOLUTE/PATH/TO/coding_mcp_server.py"]
        }
      }
    }
Claude Desktop remote (via mcp-remote):
    {
      "mcpServers": {
        "coding-tools": {
          "command": "npx",
          "args": ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"]
        }
      }
    }

Guardrails (env):
    CODING_MCP_ROOT=/path/to/workspace  Jail all paths (default: no jail, cwd)
    CODING_MCP_READ_ONLY=1              Block writes/deletes
    CODING_MCP_BLOCKED=.ssh,.aws,.env   Extra blocked path parts
    CODING_MCP_ALLOWED_COMMANDS=git,npm,pytest,python  Comma allowlist (default: permissive)
    CODING_MCP_MAX_BYTES=1048576        Max file read bytes
"""

import ast
import base64
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from turbomcp import TurboMCP

mcp = TurboMCP(name="coding-tools", version="0.3.0")

# ── guardrails config (env-driven) ─────────────────────────────────────────
_CODING_ROOT = os.getenv("CODING_MCP_ROOT", "").strip()
_CODING_ROOT_PATH = Path(_coding_root).resolve() if (_coding_root := _CODING_ROOT) else None
_READ_ONLY = os.getenv("CODING_MCP_READ_ONLY", "").lower() in ("1", "true", "yes")
_MAX_BYTES = int(os.getenv("CODING_MCP_MAX_BYTES", "1048576"))  # 1MB default read cap
_MAX_LINES = 2000
_BLOCKED_PARTS = {".ssh", ".aws", ".env", ".git/hooks", "secrets", ".gnupg"}
_BLOCKED_PARTS |= {p.strip() for p in os.getenv("CODING_MCP_BLOCKED", "").split(",") if p.strip()}
_ALLOWED_CMDS_ENV = os.getenv("CODING_MCP_ALLOWED_COMMANDS", "").strip()
_ALLOWED_CMDS = [c.strip() for c in _ALLOWED_CMDS_ENV.split(",") if c.strip()] if _ALLOWED_CMDS_ENV else None
_DENY_SHELL_PATTERNS = re.compile(r"(\|\s*sh|\b(curl|wget)\b.*\|\s*(sh|bash)|__import__|rm\s+-rf\s+/)")

def _is_blocked_path(p: Path) -> str | None:
    try:
        r = p.resolve()
    except Exception:
        r = p
    parts = set(r.parts)
    # check blocked substrings and sensitive files
    for b in _BLOCKED_PARTS:
        if b in str(r) or b in parts:
            return f"blocked path part '{b}'"
    if r.name in {".env", "id_rsa", "id_ed25519", "credentials.json"}:
        return f"blocked sensitive file '{r.name}'"
    return None

def _check_writable():
    if _READ_ONLY:
        return {"error": "Server in read-only mode (CODING_MCP_READ_ONLY=1) — writes blocked"}
    return None

def _check_path(p: Path, must_exist=False) -> dict | None:
    # jail
    if _CODING_ROOT_PATH:
        try:
            r = p.resolve()
            # allow exact root
            if r != _CODING_ROOT_PATH and _CODING_ROOT_PATH not in r.parents:
                return {"error": f"Path outside CODING_MCP_ROOT ({_CODING_ROOT_PATH}): {p}"}
        except Exception as e:
            return {"error": f"Invalid path: {e}"}
    # blocked
    reason = _is_blocked_path(p)
    if reason:
        return {"error": f"Access denied: {reason}: {p}"}
    # symlink guard
    if p.is_symlink():
        # allow symlink inside jail but warn; block if points outside
        try:
            target = p.resolve()
            if _CODING_ROOT_PATH and target != _CODING_ROOT_PATH and _CODING_ROOT_PATH not in target.parents:
                return {"error": f"Symlink points outside jail: {p} -> {target}"}
        except Exception:
            pass
    if must_exist and not p.exists():
        return {"error": f"Path not found: {p}"}
    return None

def _check_command(cmd) -> dict | None:
    s = cmd if isinstance(cmd, str) else " ".join(cmd)
    if _DENY_SHELL_PATTERNS.search(s):
        return {"error": "Blocked dangerous shell pattern"}
    if _ALLOWED_CMDS is not None:
        # check first token against allowlist
        first = shlex.split(s)[0] if isinstance(s, str) and s.strip() else ""
        base = Path(first).name
        if base not in _ALLOWED_CMDS and first not in _ALLOWED_CMDS and s.split()[0] not in _ALLOWED_CMDS:
            # allow `python -m ...` as python
            if base.startswith("python"):
                if not any(x in s for x in _ALLOWED_CMDS):
                    return {"error": f"Command not in allowlist { _ALLOWED_CMDS }: {s[:100]}"}
            else:
                return {"error": f"Command not in allowlist { _ALLOWED_CMDS }: {s[:100]}"}
    return None

# Register built-in web tools from turbomcp[web] (requires `pip install turbomcp[web]`)
try:
    mcp.register_default(names=["internet_search", "webpage_scrape"])
except ImportError:
    pass  # [web] extra not installed; skip gracefully

# ── helpers ──────────────────────────────────────────────────────────────────

def _ensure_str(v):
    if isinstance(v, str):
        return v
    return json.dumps(v, indent=2, default=str)

def _run(cmd, cwd=None, timeout=120, shell=False):
    # guardrails: check workdir jail
    if cwd:
        err = _check_path(Path(cwd))
        if err:
            return {"returncode": -1, "stdout": "", "stderr": err["error"]}
    # guardrails: command allowlist / dangerous pattern
    err = _check_command(cmd)
    if err:
        return {"returncode": -1, "stdout": "", "stderr": err["error"]}
    # enforce output cap to avoid OOM (100KB)
    try:
        # A str command with shell=False was previously handed straight to
        # subprocess.run(), which treats the *entire string* as a single
        # executable name (no argv splitting, no shell parsing) — that's
        # why "cargo check" failed with "No such file or directory". Split
        # it into argv ourselves via shlex unless the caller explicitly
        # wants real shell semantics (pipes, redirects, etc).
        if isinstance(cmd, str) and not shell:
            cmd = shlex.split(cmd)
        r = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd or os.getcwd(),
            timeout=timeout, shell=shell
        )
        # cap output
        stdout = r.stdout[:100000] if r.stdout else ""
        stderr = r.stderr[:100000] if r.stderr else ""
        if len(r.stdout or "") > 100000 or len(r.stderr or "") > 100000:
            stderr += "\n[truncated 100KB]"
        return {
            "returncode": r.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s"}
    except FileNotFoundError as e:
        return {"returncode": -1, "stdout": "", "stderr": f"Command not found: {e}"}
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}

def _resolve(path):
    # Expand and resolve, but enforce jail/blocklist at call site via _check_path
    # Keep backward compat: return string; callers must check _check_path
    p = Path(path).expanduser()
    # if relative and ROOT set, resolve against ROOT (not cwd) for deterministic jail
    if not p.is_absolute() and _CODING_ROOT_PATH:
        p = _CODING_ROOT_PATH / p
    return str(p.resolve())


# ══════════════════════════════════════════════════════════════════════════════
#  FILE OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def read_file(file_path: str, offset: int = 0, limit: int = 0):
    """Read the contents of a file with optional line-range paging.

    Args:
        file_path (str): Absolute or relative path to the file
        offset (int): Starting line number (1-indexed, use negative for tail). 0 means from start.
        limit (int): Maximum number of lines to return. 0 or omit means all lines.
    """
    path = Path(_resolve(file_path))
    if (err := _check_path(path, must_exist=False)):
        return err
    if not path.exists():
        return {"error": f"File not found: {file_path}"}
    if not path.is_file():
        return {"error": f"Not a file: {file_path}"}
    # size guard
    try:
        if path.stat().st_size > _MAX_BYTES:
            return {"error": f"File too large ({path.stat().st_size} bytes > {_MAX_BYTES} max). Use offset/limit or increase CODING_MCP_MAX_BYTES"}
    except Exception:
        pass

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"error": "Cannot read binary file as text"}
    except Exception as e:
        return {"error": str(e)}

    lines = text.splitlines(keepends=True)
    total = len(lines)

    if offset == 0:
        start = 0
    elif offset > 0:
        start = offset - 1  # 1-indexed
    else:
        start = max(0, total + offset)  # negative offset = tail

    if limit > 0:
        lines = lines[start:start + limit]
    elif offset != 0:
        lines = lines[start:]
    else:
        lines = lines[:2000]

    return {
        "file_path": str(path),
        "size_bytes": path.stat().st_size,
        "total_lines": total,
        "content": "".join(lines),
        "truncated": len(lines) < total,
    }


@mcp.tool()
def read_multiple_files(file_paths: list):
    """Read multiple files at once. Returns contents keyed by file path.

    Args:
        file_paths (list): List of file paths to read
    """
    if len(file_paths) > 20:
        return {"error": "Too many files (max 20) — request fewer at once"}
    results = {}
    for fp in file_paths:
        path = Path(_resolve(fp))
        if (err := _check_path(path)):
            results[fp] = err
            continue
        if not path.is_file():
            results[fp] = {"error": "Not found or not a file"}
            continue
        try:
            if path.stat().st_size > _MAX_BYTES:
                results[fp] = {"error": f"File too large ({path.stat().st_size} > {_MAX_BYTES})"}
                continue
            text = path.read_text(encoding="utf-8")
            results[fp] = {
                "content": text[:50000],
                "size_bytes": path.stat().st_size,
            }
        except Exception as e:
            results[fp] = {"error": str(e)}
    return results


@mcp.tool()
def write_file(file_path: str, content: str, mode: str = "overwrite"):
    """Write content to a file. Creates parent directories if needed.

    Args:
        file_path (str): Path to the file
        content (str): Text content to write
        mode (str): 'overwrite' (default) to replace, 'append' to add to end
    """
    if (err := _check_writable()):
        return err
    path = Path(_resolve(file_path))
    if (err := _check_path(path)):
        return err
    if len(content.encode("utf-8")) > _MAX_BYTES * 5:
        return {"error": f"Content too large (> { _MAX_BYTES*5} bytes)"}
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if mode == "append":
            with path.open("a", encoding="utf-8") as f:
                f.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        return {"file_path": str(path), "bytes_written": len(content.encode("utf-8")), "mode": mode}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def edit_file(file_path: str, old_string: str, new_string: str):
    """Find and replace text in a file. Uses exact string matching.

    Args:
        file_path (str): Path to the file
        old_string (str): Text to find (exact match)
        new_string (str): Replacement text
    """
    if (err := _check_writable()):
        return err
    path = Path(_resolve(file_path))
    if (err := _check_path(path)):
        return err
    if not path.is_file():
        return {"error": f"File not found: {file_path}"}

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}

    if old_string not in text:
        return {"error": "old_string not found in file", "file_path": str(path)}

    # Try exact match first, then do replacement
    count = text.count(old_string)
    new_text = text.replace(old_string, new_string, 1)
    path.write_text(new_text, encoding="utf-8")

    return {
        "file_path": str(path),
        "replacements_made": 1,
        "total_occurrences": count,
        "summary": f"Replaced 1 of {count} occurrence(s) in {path.name}",
    }


@mcp.tool()
def apply_patch(file_path: str, patch_text: str):
    """Apply a unified-diff patch to a file. The patch can be a partial diff
    with a few lines of surrounding context. Uses ``patch`` command if available,
    otherwise falls back to a simple line-level replace.

    Args:
        file_path (str): Path to the file to patch
        patch_text (str): Unified diff text (like output of ``diff -u``)
    """
    if (err := _check_writable()):
        return err
    path = _resolve(file_path)
    if (err := _check_path(Path(path))):
        return err
    if not Path(path).is_file():
        return {"error": f"File not found: {file_path}"}

    # Try using system `patch` command
    try:
        r = subprocess.run(
            ["patch", path],
            input=patch_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            return {"patched": path, "method": "patch", "stdout": r.stdout}
    except FileNotFoundError:
        pass

    # Fallback: locate hunk and apply line-based replacement
    # Parse the unified diff for the target file
    lines = patch_text.splitlines(keepends=True)
    source_lines = []
    target_lines = []
    in_hunk = False
    hunk_source = []
    hunk_target = []
    hunk_offset = 0

    for line in lines:
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            continue
        m = re.match(r"^@@ -(\d+),?\d* \+(\d+),?\d* @@", line)
        if m:
            if in_hunk and hunk_source:
                source_lines.append((hunk_offset, hunk_source, hunk_target))
            hunk_offset = int(m.group(1))
            hunk_source = []
            hunk_target = []
            in_hunk = True
            continue
        if in_hunk:
            if line.startswith("-"):
                hunk_source.append(line[1:])
            elif line.startswith("+"):
                hunk_target.append(line[1:])
            elif line.startswith(" "):
                hunk_source.append(line[1:])
                hunk_target.append(line[1:])

    if in_hunk and hunk_source:
        source_lines.append((hunk_offset, hunk_source, hunk_target))

    if not source_lines:
        return {"error": "Could not parse patch text. Provide a unified diff format."}

    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}

    file_lines = text.splitlines(keepends=True)
    changes = []

    for offset, src, tgt in reversed(source_lines):
        idx = offset - 1
        if idx < 0 or idx >= len(file_lines):
            changes.append({"offset": offset, "error": "offset out of range", "skipped": True})
            continue
        # Check if the source matches
        match_len = len(src)
        actual = file_lines[idx:idx + match_len]
        # Compare stripping line endings
        if [l.rstrip("\n\r") for l in actual] == [l.rstrip("\n\r") for l in src]:
            file_lines[idx:idx + match_len] = tgt
            changes.append({"offset": offset, "lines_changed": len(src), "lines_replaced_with": len(tgt)})
        else:
            changes.append({"offset": offset, "error": "context does not match", "skipped": True})

    if all(c.get("skipped") for c in changes):
        return {"error": "Patch does not apply cleanly. File content does not match patch context.", "changes": changes}

    Path(path).write_text("".join(file_lines), encoding="utf-8")
    return {"patched": path, "method": "line-match", "changes": [c for c in changes if not c.get("skipped")]}


@mcp.tool()
def delete_file(file_path: str):
    """Delete a file or empty directory.

    Args:
        file_path (str): Path to the file/directory to delete
    """
    if (err := _check_writable()):
        return err
    path = Path(_resolve(file_path))
    if (err := _check_path(path)):
        return err
    if not path.exists():
        return {"error": f"Path not found: {file_path}"}
    try:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
        return {"deleted": str(path)}
    except OSError as e:
        return {"error": str(e)}


@mcp.tool()
def delete_directory(dir_path: str, recursive: bool = False):
    """Delete a directory. Use recursive=True for non-empty directories.

    Args:
        dir_path (str): Path to the directory
        recursive (bool): If True, delete directory and all its contents
    """
    if (err := _check_writable()):
        return err
    path = Path(_resolve(dir_path))
    if (err := _check_path(path)):
        return err
    if not path.is_dir():
        return {"error": f"Directory not found: {dir_path}"}
    # extra guard: refuse recursive delete of ROOT or /
    if recursive and _CODING_ROOT_PATH and path.resolve() == _CODING_ROOT_PATH:
        return {"error": "Refusing to recursively delete CODING_MCP_ROOT itself"}
    try:
        if recursive:
            shutil.rmtree(path)
        else:
            path.rmdir()
        return {"deleted": str(path)}
    except OSError as e:
        return {"error": str(e)}


@mcp.tool()
def move_file(source: str, destination: str):
    """Move or rename a file or directory.

    Args:
        source (str): Current path
        destination (str): New path
    """
    if (err := _check_writable()):
        return err
    src = Path(_resolve(source))
    dst = Path(_resolve(destination))
    if (err := _check_path(src)):
        return err
    if (err := _check_path(dst)):
        return err
    if not src.exists():
        return {"error": f"Source not found: {source}"}
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return {"source": str(src), "destination": str(dst)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def copy_file(source: str, destination: str):
    """Copy a file or directory.

    Args:
        source (str): Source path
        destination (str): Destination path
    """
    src = Path(_resolve(source))
    dst = Path(_resolve(destination))
    if (err := _check_path(src)):
        return err
    if (err := _check_path(dst)):
        return err
    if not src.exists():
        return {"error": f"Source not found: {source}"}
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        return {"source": str(src), "destination": str(dst)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_directory(dir_path: str = ".", recursive: bool = False):
    """List contents of a directory with metadata.

    Args:
        dir_path (str): Directory path (default: current directory)
        recursive (bool): If True, list recursively
    """
    path = Path(_resolve(dir_path))
    if (err := _check_path(path)):
        return err
    if not path.is_dir():
        return {"error": f"Directory not found: {dir_path}"}
    # guard recursive bomb
    if recursive:
        # limit to avoid OOM on node_modules
        count = sum(1 for _ in path.rglob("*"))
        if count > 5000:
            return {"error": f"Directory too large ({count} entries > 5000). Use non-recursive or narrower path", "directory": str(path)}

    def _entry(p):
        s = p.stat()
        return {
            "name": p.name,
            "path": str(p),
            "type": "directory" if p.is_dir() else "file",
            "size_bytes": s.st_size if p.is_file() else 0,
            "modified": datetime.fromtimestamp(s.st_mtime, tz=timezone.utc).isoformat(),
        }

    if recursive:
        results = []
        for p in sorted(path.rglob("*")):
            results.append(_entry(p))
    else:
        results = [_entry(p) for p in sorted(path.iterdir())]

    return {"directory": str(path), "entries": results, "count": len(results)}


@mcp.tool()
def create_directory(dir_path: str):
    """Create a directory (and all parent directories).

    Args:
        dir_path (str): Path of the directory to create
    """
    if (err := _check_writable()):
        return err
    path = Path(_resolve(dir_path))
    if (err := _check_path(path)):
        return err
    path.mkdir(parents=True, exist_ok=True)
    return {"created": str(path)}


# ══════════════════════════════════════════════════════════════════════════════
#  SEARCH
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_files(pattern: str, root: str = ".", max_results: int = 50):
    """Find files matching a glob pattern.

    Args:
        pattern (str): Glob pattern (e.g. '**/*.py', '**/test_*.py')
        root (str): Root directory to search in
        max_results (int): Max number of results
    """
    root_path = Path(_resolve(root))
    if (err := _check_path(root_path)):
        return err
    if not root_path.is_dir():
        return {"error": f"Directory not found: {root}"}
    max_results = min(max_results, 100)

    results = []
    for p in sorted(root_path.rglob(pattern)):
        if p.is_file():
            results.append({
                "path": str(p),
                "name": p.name,
                "size_bytes": p.stat().st_size,
            })
            if len(results) >= max_results:
                break

    return {"root": str(root_path), "pattern": pattern, "results": results, "count": len(results)}


@mcp.tool()
def grep_search(pattern: str, root: str = ".", include: str = "", max_results: int = 50):
    """Search for text/regex patterns inside file contents.

    Args:
        pattern (str): Regex pattern to search for
        root (str): Root directory to search in
        include (str): File glob pattern to filter (e.g. '*.py', '*.{ts,js}')
        max_results (int): Max matches to return
    """
    root_path = Path(_resolve(root))
    if (err := _check_path(root_path)):
        return err
    if not root_path.is_dir():
        return {"error": f"Directory not found: {root}"}
    max_results = min(max_results, 100)

    flags = ["-rn", "--color=never", "--max-count=5"]
    if include:
        flags.extend(["--include", include])
    flags.extend(["-e", pattern, str(root_path)])

    try:
        r = subprocess.run(
            ["grep"] + flags, capture_output=True, text=True, timeout=30
        )
        output = r.stdout[:100000]
        lines = output.splitlines()[:max_results]
        return {
            "root": str(root_path),
            "pattern": pattern,
            "results": lines,
            "count": len(lines),
        }
    except FileNotFoundError:
        # fallback to Python-based search
        results = []
        for p in root_path.rglob("*"):
            if not p.is_file():
                continue
            if include and not Path(p.name).match(include):
                continue
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern in line or __import__("re").search(pattern, line):
                        results.append(f"{p}:{i}:{line[:200]}")
                        if len(results) >= max_results:
                            break
            except Exception:
                continue
            if len(results) >= max_results:
                break
        return {"root": str(root_path), "pattern": pattern, "results": results, "count": len(results)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def search_and_replace(pattern: str, replacement: str, include: str = "*", root: str = ".", dry_run: bool = False):
    """Search and replace text across multiple files using regex.

    Args:
        pattern (str): Regex pattern to search for
        replacement (str): Replacement text
        include (str): Glob pattern for files to include (e.g. '*.py', '*.{ts,js}')
        root (str): Root directory to search in
        dry_run (bool): If True, only show matches without making changes
    """
    if not dry_run and (err := _check_writable()):
        return err
    root_path = Path(_resolve(root))
    if (err := _check_path(root_path)):
        return err
    if not root_path.is_dir():
        return {"error": f"Directory not found: {root}"}

    changed = []
    errors = []
    rx = re.compile(pattern)

    for p in root_path.rglob(include):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if not rx.search(text):
            continue

        new_text = rx.sub(replacement, text)
        if new_text == text:
            continue

        count = text.count(new_text)  # not quite right, let's count matches
        count = len(rx.findall(text))
        changed.append({"file": str(p), "matches": count})

        if not dry_run:
            p.write_text(new_text, encoding="utf-8")

    return {
        "pattern": pattern,
        "replacement": replacement,
        "root": str(root_path),
        "dry_run": dry_run,
        "changed_files": changed,
        "total_files": len(changed),
        "errors": errors,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  CODE EXECUTION & TOOLS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def run_command(command: str, workdir: str = "", timeout: int = 120, shell: bool = False):
    """Execute a shell command. Use for running tests, builds, linters, etc.

    Args:
        command (str): Shell command to run
        workdir (str): Working directory (default: current)
        timeout (int): Timeout in seconds (default 120)
        shell (bool): Set True if the command needs real shell features
            (pipes '|', redirects '>', '&&', globbing). Defaults to False,
            which splits the command into argv safely via shlex — fine for
            plain commands like "cargo check" or "npm run build".
    """
    result = _run(command, cwd=workdir or None, timeout=timeout, shell=shell)
    return result


@mcp.tool()
def run_python(code: str, workdir: str = ""):
    """Execute Python code and return stdout/stderr.

    Args:
        code (str): Python code to execute
        workdir (str): Working directory
    """
    cwd = workdir or os.getcwd()
    result = _run([sys.executable, "-c", code], cwd=cwd, timeout=60)
    return result


@mcp.tool()
def run_python_file(file_path: str, args: str = "", workdir: str = ""):
    """Execute a Python script file.

    Args:
        file_path (str): Path to the Python file
        args (str): Command-line arguments to pass
        workdir (str): Working directory
    """
    path = _resolve(file_path)
    if (err := _check_path(Path(path))):
        return err
    if not Path(path).is_file():
        return {"error": f"File not found: {file_path}"}
    cwd = workdir or os.getcwd()
    cmd = [sys.executable, path] + (args.split() if args else [])
    result = _run(cmd, cwd=cwd, timeout=120)
    return result


@mcp.tool()
def run_test(command: str = "", workdir: str = "", framework: str = "auto"):
    """Run tests in a project. Auto-detects pytest, unittest, npm test, etc.

    Args:
        command (str): Explicit test command (if empty, auto-detect)
        workdir (str): Working directory (default: current)
        framework (str): 'auto', 'pytest', 'unittest', 'npm', 'make'
    """
    cwd = workdir or os.getcwd()
    if command:
        return _run(command, cwd=cwd, timeout=300)

    # Auto-detect
    if framework == "auto":
        cwd_path = Path(cwd)
        if (cwd_path / "pyproject.toml").exists() or (cwd_path / "setup.py").exists() or (cwd_path / "setup.cfg").exists():
            if (cwd_path / "pytest.ini").exists() or (cwd_path / "pyproject.toml").read_text().find("pytest") >= 0:
                command = "python -m pytest"
            else:
                command = "python -m unittest discover"
        elif (cwd_path / "package.json").exists():
            command = "npm test"
        elif (cwd_path / "Makefile").exists():
            command = "make test"
        elif (cwd_path / "Cargo.toml").exists():
            command = "cargo test"
        else:
            command = "python -m pytest"
    elif framework == "pytest":
        command = "python -m pytest"
    elif framework == "unittest":
        command = "python -m unittest discover"
    elif framework == "npm":
        command = "npm test"

    return _run(command, cwd=cwd, timeout=300)


@mcp.tool()
def process_manage(
    action: str,
    command: str = "",
    name: str = "",
    workdir: str = "",
    timeout: int = 3600,
):
    """Manage long-running background processes (dev servers, watchers, etc).

    Args:
        action (str): 'start' to begin a background process, 'list' to show running, 'stop' to kill one, 'stop_all' to kill all
        command (str): Shell command to run (required for 'start')
        name (str): Name for the process (used with 'start' and 'stop')
        workdir (str): Working directory
        timeout (int): Max runtime in seconds before auto-kill (default 3600)
    """
    if not hasattr(process_manage, "_procs"):
        process_manage._procs = {}

    if action == "list":
        alive = {}
        for n, info in list(process_manage._procs.items()):
            p = info["popen"]
            ret = p.poll()
            if ret is not None:
                alive[n] = {"status": "exited", "returncode": ret}
            else:
                alive[n] = {
                    "status": "running",
                    "pid": p.pid,
                    "command": info["command"],
                    "started": info["started"],
                    "workdir": info.get("workdir", ""),
                }
        return {"processes": alive}

    if action == "stop":
        if name not in process_manage._procs:
            return {"error": f"No process named '{name}'"}
        info = process_manage._procs[name]
        p = info["popen"]
        if p.poll() is None:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        del process_manage._procs[name]
        return {"stopped": name}

    if action == "stop_all":
        stopped = []
        for n in list(process_manage._procs.keys()):
            info = process_manage._procs[n]
            p = info["popen"]
            if p.poll() is None:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            stopped.append(n)
        process_manage._procs = {}
        return {"stopped": stopped}

    if action == "start":
        if not command:
            return {"error": "command is required for start"}
        if (err := _check_command(command)):
            return err
        if workdir and (err := _check_path(Path(workdir))):
            return err
        proc_name = name or command.split()[0]
        if proc_name in process_manage._procs:
            p = process_manage._procs[proc_name]["popen"]
            if p.poll() is None:
                return {"error": f"Process '{proc_name}' is already running (pid {p.pid})"}

        cwd = workdir or os.getcwd()
        # guard: limit concurrent processes
        running = sum(1 for info in process_manage._procs.values() if info["popen"].poll() is None)
        if running >= 5:
            return {"error": "Too many background processes (max 5)"}
        p = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            preexec_fn=os.setsid,
        )
        process_manage._procs[proc_name] = {
            "popen": p,
            "command": command,
            "started": datetime.now(timezone.utc).isoformat(),
            "workdir": cwd,
            "timeout": timeout,
        }
        return {
            "started": proc_name,
            "pid": p.pid,
            "command": command,
            "workdir": cwd,
        }

    return {"error": f"Unknown action: {action}. Use start, list, stop, or stop_all."}


# ══════════════════════════════════════════════════════════════════════════════
#  CODE ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def analyze_code(file_path: str):
    """Get structural info about a Python file: classes, functions, imports.

    Args:
        file_path (str): Path to Python file
    """
    path = Path(_resolve(file_path))
    if (err := _check_path(path)):
        return err
    if not path.is_file():
        return {"error": f"File not found: {file_path}"}

    try:
        tree = compile(path.read_text(encoding="utf-8"), str(path), "exec", ast.PyCF_ONLY_AST)
    except SyntaxError as e:
        return {"error": f"Syntax error: {e}"}
    except Exception as e:
        return {"error": str(e)}

    info = {
        "file_path": str(path),
        "imports": [],
        "classes": [],
        "functions": [],
        "lines": path.stat().st_size,
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                info["imports"].append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                info["imports"].append(f"{module}.{alias.name}")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            info["classes"].append({
                "name": node.name,
                "lineno": node.lineno,
                "methods": methods,
            })
        elif isinstance(node, ast.FunctionDef):
            info["functions"].append({
                "name": node.name,
                "lineno": node.lineno,
                "args": [a.arg for a in node.args.args],
            })

    return info


@mcp.tool()
def get_ast(file_path: str):
    """Get the AST representation of a Python file (for deeper analysis).

    Args:
        file_path (str): Path to Python file
    """
    path = Path(_resolve(file_path))
    if (err := _check_path(path)):
        return err
    if not path.is_file():
        return {"error": f"File not found: {file_path}"}

    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return {"ast": ast.dump(tree, indent=2)}
    except SyntaxError as e:
        return {"error": f"Syntax error: {e}"}


@mcp.tool()
def format_code(file_path: str, check_only: bool = False):
    """Format Python code using autopep8 or black (whichever is available).

    Args:
        file_path (str): Path to the file to format
        check_only (bool): If True, only check if formatting is needed
    """
    if not check_only and (err := _check_writable()):
        return err
    path = _resolve(file_path)
    if (err := _check_path(Path(path))):
        return err
    # Try black first, then autopep8
    for formatter, check_flag in [("black", "--check"), ("autopep8", "--diff")]:
        try:
            if check_only:
                cmd = [formatter, check_flag, path]
            else:
                cmd = [formatter, path] if formatter == "black" else [formatter, "--in-place", path]
            result = _run(cmd, timeout=30)
            return {
                "formatter": formatter,
                "check_only": check_only,
                "needs_formatting": result["returncode"] != 0 if check_only else False,
                "stdout": result["stdout"],
                "stderr": result["stderr"],
            }
        except FileNotFoundError:
            continue
    return {"error": "No formatter found. Install black or autopep8."}


@mcp.tool()
def lint_code(file_paths: list):
    """Lint Python files using ruff or pylint.

    Args:
        file_paths (list): List of file paths to lint
    """
    for linter in ["ruff", "pylint", "pyflakes", "pycodestyle"]:
        try:
            cmd = [linter] + file_paths
            result = _run(cmd, timeout=30)
            return {
                "linter": linter,
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "returncode": result["returncode"],
            }
        except FileNotFoundError:
            continue
    return {"error": "No linter found. Install ruff or pylint."}


# ══════════════════════════════════════════════════════════════════════════════
#  GIT INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def git_status(workdir: str = ""):
    """Show git status of the repository.

    Args:
        workdir (str): Working directory (default: current)
    """
    cwd = workdir or os.getcwd()
    return _run(["git", "status"], cwd=cwd)


@mcp.tool()
def git_log(max_count: int = 10, workdir: str = ""):
    """Show recent git commit log.

    Args:
        max_count (int): Number of commits to show
        workdir (str): Working directory (default: current)
    """
    cwd = workdir or os.getcwd()
    return _run(
        ["git", "log", f"--max-count={max_count}", "--oneline", "--decorate"],
        cwd=cwd,
    )


@mcp.tool()
def git_diff(staged: bool = False, workdir: str = ""):
    """Show git diff (unstaged or staged changes).

    Args:
        staged (bool): If True, show staged changes
        workdir (str): Working directory (default: current)
    """
    cwd = workdir or os.getcwd()
    cmd = ["git", "diff"]
    if staged:
        cmd.append("--staged")
    return _run(cmd, cwd=cwd)


@mcp.tool()
def git_commit(message: str, workdir: str = ""):
    """Stage all changes and commit.

    Args:
        message (str): Commit message
        workdir (str): Working directory (default: current)
    """
    cwd = workdir or os.getcwd()
    result = _run(["git", "add", "."], cwd=cwd)
    if result["returncode"] != 0:
        return result
    return _run(["git", "commit", "-m", message], cwd=cwd)


@mcp.tool()
def git_add(files: list, workdir: str = ""):
    """Stage specific files for commit.

    Args:
        files (list): List of file paths to stage
        workdir (str): Working directory (default: current)
    """
    cwd = workdir or os.getcwd()
    return _run(["git", "add"] + files, cwd=cwd)


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITY / SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_working_dir():
    """Get the current working directory."""
    return {"cwd": os.getcwd()}


@mcp.tool()
def read_image_file(file_path: str):
    """Read an image file and return base64 data. Claude Desktop will display the image directly.

    Args:
        file_path (str): Path to the image file (png, jpg, gif, svg, webp, etc.)
    """
    path = Path(_resolve(file_path))
    if (err := _check_path(path)):
        return err
    if not path.is_file():
        return {"error": f"File not found: {file_path}"}
    if path.stat().st_size > _MAX_BYTES * 5:
        return {"error": f"Image too large ({path.stat().st_size} > { _MAX_BYTES*5 })"}

    ext = path.suffix.lower().lstrip(".")
    mime_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "svg": "image/svg+xml",
        "webp": "image/webp",
        "bmp": "image/bmp",
        "ico": "image/x-icon",
    }
    mime = mime_map.get(ext, "application/octet-stream")

    try:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception as e:
        return {"error": str(e)}

    return {
        "file_path": str(path),
        "mime_type": mime,
        "data": b64,
        "size_bytes": path.stat().st_size,
    }


@mcp.tool()
def get_file_info(file_path: str):
    """Get metadata about a file or directory.

    Args:
        file_path (str): Path to the file or directory
    """
    path = Path(_resolve(file_path))
    if (err := _check_path(path)):
        return err
    if not path.exists():
        return {"error": "Path not found"}
    s = path.stat()
    return {
        "path": str(path),
        "type": "directory" if path.is_dir() else "file",
        "size_bytes": s.st_size,
        "created": datetime.fromtimestamp(s.st_ctime, tz=timezone.utc).isoformat(),
        "modified": datetime.fromtimestamp(s.st_mtime, tz=timezone.utc).isoformat(),
        "permissions": oct(stat.S_IMODE(s.st_mode)),
        "is_symlink": path.is_symlink(),
    }


@mcp.tool()
def get_workspace_info(root: str = "."):
    """Get info about a project workspace: dir structure, git status, configs.

    Args:
        root (str): Root directory of the workspace
    """
    root_path = Path(_resolve(root))
    if (err := _check_path(root_path)):
        return err
    if not root_path.is_dir():
        return {"error": "Directory not found"}

    git = _run(["git", "rev-parse", "--show-toplevel"], cwd=str(root_path))
    git_root = git["stdout"].strip() if git["returncode"] == 0 else ""

    # Check for common project files
    project_files = {}
    for fname in ["package.json", "pyproject.toml", "setup.py", "setup.cfg",
                  "Cargo.toml", "go.mod", "Makefile", "Dockerfile",
                  "README.md", ".gitignore", "composer.json"]:
        fp = root_path / fname
        if fp.exists():
            project_files[fname] = str(fp)

    return {
        "root": str(root_path),
        "git_root": git_root or None,
        "is_git_repo": bool(git_root),
        "project_files": project_files,
        "entry_count": len(list(root_path.iterdir())),
    }


@mcp.tool()
def get_current_time(tz_name: str = ""):
    """Get the current date and time.

    Args:
        tz_name (str): Optional IANA timezone name (e.g. 'Asia/Kolkata')
    """
    if tz_name:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tz_name))
    else:
        now = datetime.now().astimezone()
    utc_now = datetime.now(timezone.utc)
    return {
        "local_time": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "utc_time": utc_now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "timestamp": int(now.timestamp()),
        "day_of_week": now.strftime("%A"),
        "timezone": tz_name or str(now.tzinfo),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  PACKAGE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def install_package(packages: list, manager: str = "auto", workdir: str = ""):
    """Install packages using pip, npm, or cargo.

    Args:
        packages (list): Package names to install
        manager (str): 'auto', 'pip', 'npm', 'cargo'
        workdir (str): Working directory
    """
    if _READ_ONLY:
        return {"error": "install_package blocked in read-only mode"}
    if workdir and (err := _check_path(Path(workdir))):
        return err
    cwd = workdir or os.getcwd()
    if manager == "auto":
        if (Path(cwd) / "package.json").exists():
            manager = "npm"
        elif (Path(cwd) / "Cargo.toml").exists():
            manager = "cargo"
        else:
            manager = "pip"

    if manager == "pip":
        cmd = [sys.executable, "-m", "pip", "install"] + packages
    elif manager == "npm":
        cmd = ["npm", "install"] + packages
    elif manager == "cargo":
        cmd = ["cargo", "add"] + packages
    else:
        return {"error": f"Unknown package manager: {manager}"}

    return _run(cmd, cwd=cwd, timeout=180)


# ══════════════════════════════════════════════════════════════════════════════
#  SERVER LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def restart_server():
    """Restart this MCP server process in-place after editing this file's
    source (e.g. adding or fixing a tool). Re-execs the current process with
    its original argv via os.execv, which preserves open file descriptors —
    since this server talks over stdio, the client's connection survives the
    restart transparently and the new code is live on the very next call.
    There is nothing to await: the process image is replaced immediately, so
    this call never returns a normal result.
    """
    python = sys.executable
    os.execv(python, [python] + sys.argv)


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT — stdio (default) or Streamable HTTP (turbomcp 0.3.0)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Direct run supports both transports:
    #   python coding_mcp_server.py              -> stdio
    #   python coding_mcp_server.py --http       -> http 127.0.0.1:8000
    #   python coding_mcp_server.py --http --port 8001 --host 0.0.0.0 --no-auth
    # Preferred for MCP clients: `turbomcp serve coding_mcp_server.py [--http]`
    if "--http" in sys.argv:
        try:
            from turbomcp.mcp_http import serve_http
        except ImportError:
            print("turbomcp[http] not installed: pip install turbomcp[http]", file=sys.stderr)
            sys.exit(1)
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("--http", action="store_true")
        p.add_argument("--host", default=os.getenv("CODING_MCP_HOST", "127.0.0.1"))
        p.add_argument("--port", type=int, default=int(os.getenv("CODING_MCP_PORT", "8000")))
        p.add_argument("--no-auth", action="store_true", default=os.getenv("CODING_MCP_NO_AUTH", "").lower() in ("1","true","yes"))
        args, _ = p.parse_known_args()
        # jailing info
        if _CODING_ROOT_PATH:
            print(f"coding_mcp_server: jail root {_CODING_ROOT_PATH} read_only={_READ_ONLY}", file=sys.stderr)
        serve_http(mcp, host=args.host, port=args.port, require_auth=not args.no_auth)
    else:
        if _CODING_ROOT_PATH:
            print(f"coding_mcp_server: jail root {_CODING_ROOT_PATH} read_only={_READ_ONLY}", file=sys.stderr)
        from turbomcp.mcp_stdio import serve_stdio
        serve_stdio(mcp)
