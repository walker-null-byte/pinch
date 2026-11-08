"""Session store: one JSONL file per session, appended and flushed after every step.

Each line is {"type": ..., "ts": ..., ...}. Types:
  meta     repo, model, tool mode, style, confidential flag (first line)
  message  one chat message exactly as sent to / received from the model (no system prompt)
  ledger   one record per model call (see ledger.py)
  event    approvals, blocks, style changes, failover notices, errors

`pinch --resume <id>` rebuilds the conversation from the `message` lines. Because every line is
flushed to disk as it happens, a crash loses at most the step in flight.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def find_session(session_dir: Path, ref: str) -> Path:
    """Accept a full path, a session id, or a unique id prefix."""
    direct = Path(ref).expanduser()
    if direct.is_file():
        return direct
    matches = sorted(session_dir.glob(f"{ref}*.jsonl")) if ref else []
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"pinch: no session '{ref}' in {session_dir}")
    raise SystemExit(f"pinch: '{ref}' matches {len(matches)} sessions; give more of the id")


class Session:
    def __init__(self, path: Path):
        self.path = path
        self.id = path.stem

    @classmethod
    def create(cls, session_dir: Path, meta: dict) -> "Session":
        session_dir.mkdir(parents=True, exist_ok=True)
        session = cls(session_dir / f"{new_session_id()}.jsonl")
        session.append("meta", **meta)
        return session

    def append(self, type_: str, **fields) -> None:
        line = json.dumps({"type": type_, "ts": round(time.time(), 3), **fields}, default=str)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def records(self) -> list[dict]:
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a half-written last line after a crash; ignore it
        return out

    # -- helpers used by resume --------------------------------------------------------------

    def meta(self) -> dict:
        for r in self.records():
            if r["type"] == "meta":
                return r
        raise SystemExit(f"pinch: {self.path} is not a pinch session (no meta line)")

    def messages(self) -> list[dict]:
        """Rebuild the chat history as it was stored, with later additions to tool results (type `patch`) applied."""
        messages = []
        for r in self.records():
            if r["type"] == "message":
                messages.append(dict(r["message"]))
            elif r["type"] == "patch":
                for m in reversed(messages):
                    if m["role"] == "tool" and m["tool_call_id"] == r["tool_call_id"]:
                        m["content"] += r["append"]
                        break
        return messages

    def last_served(self) -> tuple | None:
        """(provider, served model) of the most recent model call, to spot a change after resume."""
        rows = [r for r in self.records() if r["type"] == "ledger"]
        return (rows[-1].get("provider"), rows[-1].get("served_model")) if rows else None

    def next_step(self) -> int:
        return 1 + sum(1 for r in self.records() if r["type"] == "ledger")

    def last_style(self) -> str | None:
        styles = [r["style"] for r in self.records() if r["type"] == "event" and r.get("event") == "style"]
        return styles[-1] if styles else None


def missing_tool_results(messages: list[dict]) -> list[dict]:
    """After a crash, the last assistant message may ask for tools that never ran. Return tool
    messages saying so, to append (and store), so the history stays valid for the API."""
    last_assistant = max((i for i, m in enumerate(messages) if m["role"] == "assistant"), default=None)
    if last_assistant is None:
        return []
    answered = {m.get("tool_call_id") for m in messages[last_assistant + 1:] if m["role"] == "tool"}
    return [
        {"role": "tool", "tool_call_id": c["id"], "content": "Error: the session was interrupted before this tool ran. It did not run."}
        for c in messages[last_assistant].get("tool_calls") or []
        if c["id"] not in answered
    ]
