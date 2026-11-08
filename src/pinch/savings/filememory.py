"""Technique `filememory`: the model should not pay twice for a file it has already seen.

Files are tracked by content hash. Reading a file again returns:
  - unchanged since last seen  -> a one-line stub
  - changed since last seen    -> a diff against the version the model saw (or the whole file if the diff is no smaller)
Only whole-file reads are tracked; partial reads (offset/limit) pass through untouched.
The message that carries a file's latest version is protected from history trimming.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Seen:
    content: str
    sha: str
    step: int
    holders: list[str] = field(default_factory=list)  # tool_call_ids of messages that carry this file's latest version


class FileMemory:
    def __init__(self, repo: Path):
        self.repo = Path(os.path.realpath(repo))
        self.seen: dict[str, Seen] = {}

    def _rel(self, path: str) -> str:
        p = Path(os.path.realpath(self.repo / path))
        return p.relative_to(self.repo).as_posix() if p.is_relative_to(self.repo) else str(p)

    def note_seen(self, rel_path: str, content: str, step: int, holder: str = "") -> None:
        """The model was shown this whole file (for example in the first prompt)."""
        self.seen[rel_path] = Seen(content, hashlib.sha256(content.encode()).hexdigest(), step, [holder] if holder else [])

    def protected(self) -> set[str]:
        return {h for s in self.seen.values() for h in s.holders}

    def process_read(self, args: dict, result: str, step: int, holder: str) -> str:
        """Rewrite the text a read_file call is about to return, if we can say it in fewer words."""
        if args.get("offset", 0) or args.get("limit", 0):
            return result
        try:
            data = json.loads(result)
            content, path = data["content"], data["file_path"]
        except (ValueError, KeyError, TypeError):
            return result
        if data.get("truncated"):
            return result
        rel = self._rel(path)
        sha = hashlib.sha256(content.encode()).hexdigest()
        before = self.seen.get(rel)
        if before is None:
            self.note_seen(rel, content, step, holder)
            return result
        if before.sha == sha:
            return f"[pinch: {rel} is unchanged since step {before.step}. Not sent again.]"
        diff = "\n".join(difflib.unified_diff(before.content.splitlines(), content.splitlines(),
                                              f"{rel} (step {before.step})", f"{rel} (now)", lineterm="", n=2))
        holders = before.holders + [holder]
        self.seen[rel] = Seen(content, sha, step, holders)
        if len(diff) >= len(content):
            self.seen[rel].holders = [holder]  # the whole file is in this message, older ones are not needed
            return result
        return f"[pinch: {rel} changed since step {before.step}. Diff against the version you saw:]\n{diff}"
