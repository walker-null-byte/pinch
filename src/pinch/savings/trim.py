"""Technique `trim`: old tool output becomes a one-line stub.

Applied to the copy of the history that is sent to the model; the session file keeps everything. A tool result
is stubbed once it is more than `keep` model steps old, unless it carries the latest version of a file
(see filememory). The stub depends only on the message and its age, so the same message becomes the same stub
every time, and the part of the request before it stays byte-identical for provider prompt caching.

File reads are the exception: an old read_file result is stubbed only once a later read of the same file shows
the same lines again. Stubbing every old read made a small model on a large file reread the same lines in a loop
(set B, first benchmark run): it cut the code the model was reasoning about and told it to run the read again.
"""

from __future__ import annotations

import json

MIN_CHARS = 200  # shorter results are not worth a stub


def trim_history(messages: list[dict], keep: int, protected: set[str]) -> list[dict]:
    calls: dict[str, str] = {}
    spans: dict[str, tuple[str, int, float]] = {}  # tool_call_id -> (file, first line, last line) of a read_file call
    for m in messages:
        for c in m.get("tool_calls") or []:
            calls[c["id"]] = _describe(c)
            span = _read_span(c)
            if span:
                spans[c["id"]] = span
    shown_later: list[tuple[str, int, float]] = []  # lines that later, complete reads show again
    assistant_after = 0
    out = list(messages)
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m["role"] == "assistant":
            assistant_after += 1
            continue
        if m["role"] != "tool":
            continue
        span = spans.get(m["tool_call_id"])
        if span and _cut_short(m["content"]):
            span = None  # this read did not return everything it asked for, so it covers nothing and is treated like other output
        old = assistant_after > keep and m["tool_call_id"] not in protected and len(m["content"]) > MIN_CHARS
        if span:
            if old and any(f == span[0] and a <= span[1] and b >= span[2] for f, a, b in shown_later):
                out[i] = {**m, "content": f"[pinch: earlier read of {span[0]} cut; a later read shows the same lines.]"}
            shown_later.append(span)
        elif old:
            what = calls.get(m["tool_call_id"], "a tool call")
            out[i] = {**m, "content": f"[pinch: earlier output of {what} cut, {len(m['content'])} characters. Run it again if you need it.]"}
    return out


def _args(call: dict) -> tuple[str, dict]:
    fn = call.get("function", {})
    name, raw = fn.get("name", "?"), fn.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (ValueError, TypeError):
        args = {}
    if name == "call_tool":
        name, inner = args.get("name", "?"), args.get("arguments")
        try:
            args = json.loads(inner) if isinstance(inner, str) else dict(inner or {})
        except (ValueError, TypeError):
            args = {}
    return name, args if isinstance(args, dict) else {}


def _read_span(call: dict) -> tuple[str, int, float] | None:
    name, args = _args(call)
    path = args.get("file_path") or args.get("path")
    if name != "read_file" or not path:
        return None
    try:
        offset, limit = int(args.get("offset") or 0), int(args.get("limit") or 0)
    except (TypeError, ValueError):
        return None
    if offset < 0:
        return None  # a tail read: its lines depend on the file length
    first = max(offset, 1)
    return str(path).removeprefix("./"), first, first + limit - 1 if limit > 0 else float("inf")


def _cut_short(content: str) -> bool:
    """True when this result does not hold the lines it asked for: an error, a cut, or a filememory stub or diff."""
    return content.startswith(("Error", "[pinch:", '{"error"')) or "[output cut:" in content or '"truncated":true' in content.replace(" ", "")


def _describe(call: dict) -> str:
    name, args = _args(call)
    target = args.get("file_path") or args.get("command") or args.get("pattern") or args.get("dir_path") or args.get("query") or ""
    return f"{name} {str(target)[:60]}".strip()
