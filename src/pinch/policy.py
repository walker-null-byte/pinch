"""Confidentiality policy.

A repo is confidential if it (or a parent folder) holds a `.pinch-confidential` marker,
OR it is listed in `confidential_repos` in ~/.config/pinch/config.toml. Either is enough.

The policy is read once at startup and never re-read, so deleting the marker during a
session does not change it. On top of that, tool calls that target the marker are
blocked, and if anything still removes or edits it, it is restored from memory.

Phase 1 only computes the flag. Phase 2 uses it to pick the local-only gateway key.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

MARKER_NAME = ".pinch-confidential"

# Tool arguments that hold a path. Used to see whether a call targets the marker.
_PATH_ARGS = ("file_path", "dir_path", "source", "destination", "root", "files", "file_paths")
# Tools that can remove or move a whole folder, so a path ABOVE the marker is also a hit.
_FOLDER_TOOLS = {"delete_directory", "move_file"}


@dataclass(frozen=True)
class Policy:
    repo: Path
    confidential: bool
    reasons: tuple[str, ...] = ()
    markers: dict[Path, bytes] = field(default_factory=dict)  # marker path -> content at startup

    # -- blocking ---------------------------------------------------------------------------

    def block_reason(self, tool: str, args: dict, kind: str) -> str | None:
        """Return a plain-language reason if this call must never run, else None.

        `kind` is the gate class (read, write, shell, git, unknown). Reads of the marker are fine.
        """
        if kind == "read":
            return None
        protected = list(self.markers) or [self.repo / MARKER_NAME]

        if kind in ("shell", "unknown"):
            for value in _string_values(args):
                if MARKER_NAME in value:
                    return _MSG
        for marker in protected:
            for raw in _path_args(args):
                target = self._resolve(raw)
                if target == marker or (tool in _FOLDER_TOOLS and marker.is_relative_to(target)):
                    return _MSG
            if tool == "search_and_replace" and fnmatch(MARKER_NAME, str(args.get("include", "*"))):
                return _MSG + " Use a narrower 'include' pattern."
        return None

    def _resolve(self, raw: str) -> Path:
        return Path(os.path.realpath(self.repo / os.path.expanduser(raw)))

    # -- backstop ---------------------------------------------------------------------------

    def restore_markers(self) -> list[Path]:
        """Put back any marker that a tool removed or changed. Returns the ones restored."""
        restored = []
        for marker, content in self.markers.items():
            try:
                intact = marker.read_bytes() == content
            except OSError:
                intact = False
            if not intact:
                marker.write_bytes(content)
                restored.append(marker)
        return restored


_MSG = "Blocked: the .pinch-confidential marker protects this repo's privacy and cannot be changed or deleted."


def _string_values(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _string_values(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _string_values(v)]
    return []


def _path_args(args: dict) -> list[str]:
    out = []
    for name in _PATH_ARGS:
        out.extend(_string_values(args.get(name)))
    return out


def load_policy(repo: Path, config_path: Path | None = None, listed: tuple[str, ...] = ()) -> Policy:
    """Decide once, at startup, whether `repo` is confidential.

    `listed` is the confidential_repos list already read from config. If a config file path is
    given instead (tests, tools), it is read here.
    """
    repo = Path(os.path.realpath(repo))
    if config_path is not None and not listed:
        try:
            listed = tuple(tomllib.loads(config_path.read_text(encoding="utf-8")).get("confidential_repos", ()))
        except (OSError, tomllib.TOMLDecodeError):
            listed = ()

    reasons: list[str] = []
    markers: dict[Path, bytes] = {}
    for folder in (repo, *repo.parents):
        candidate = folder / MARKER_NAME
        if candidate.is_file():
            markers[candidate] = candidate.read_bytes()
            reasons.append(f"marker file {candidate}")
    for entry in listed:
        root = Path(os.path.realpath(Path(entry).expanduser()))
        if repo == root or repo.is_relative_to(root):
            reasons.append(f"listed in config as {entry}")
    return Policy(repo=repo, confidential=bool(reasons), reasons=tuple(reasons), markers=markers)
