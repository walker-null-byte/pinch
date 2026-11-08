"""A tiny local OpenAI-compatible server that replays scripted responses. For tests and demos.

Script (JSON, or YAML if PyYAML is installed). The top level is a list of responses, or an
object {"responses": [...]}. The Nth request to /v1/chat/completions gets the Nth response:

    [
      {"tool_calls": [{"name": "read_file", "arguments": {"file_path": "calc.py"}}]},
      {"status": 429, "message": "rate limited", "headers": {"Retry-After": "1"}},
      {"status": 500, "times": 2},
      {"content": "Done.", "provider": "mock-b", "model": "mock-large",   # "extra_fields": {...} is passed through too
       "usage": {"prompt_tokens": 120, "completion_tokens": 8, "cached_tokens": 100}}
    ]

An entry with "status" other than 200 returns that HTTP error; "times": N repeats an entry N times.
Without "usage", token counts are estimated from the request size (about 4 characters per token),
so bigger requests show up as bigger numbers. When the script runs out the server answers 500.

Every request is recorded (with secret header values replaced by a short fingerprint) in memory and,
if `record_path` is set, appended to a JSONL file, so tests can prove what was or was not sent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SECRET_WORDS = ("authorization", "key", "token", "secret", "-vk")


def load_script(source) -> list[dict]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        text = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError:
                raise SystemExit("pinch-mock: YAML scripts need PyYAML (pip install pyyaml). Or use a JSON script.")
            source = yaml.safe_load(text)
        else:
            source = json.loads(text)
    responses = source["responses"] if isinstance(source, dict) else source
    expanded = []
    for entry in responses:
        expanded.extend([entry] * int(entry.get("times", 1)))
    return expanded


def redact_headers(headers) -> dict:
    out = {}
    for name, value in headers.items():
        if any(w in name.lower() for w in _SECRET_WORDS):
            out[name.lower()] = "redacted:" + hashlib.sha256(value.encode()).hexdigest()[:8]
        else:
            out[name.lower()] = value
    return out


class MockProvider:
    def __init__(self, script, record_path: Path | str | None = None, host: str = "127.0.0.1", port: int = 0):
        self.script = script if callable(script) else load_script(script)
        self.record_path = Path(record_path) if record_path else None
        self.recorded: list[dict] = []
        self._lock = threading.Lock()
        self._calls = 0
        self._server = ThreadingHTTPServer((host, port), self._make_handler())
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def start(self) -> "MockProvider":
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def reset(self, script) -> None:
        """Swap in a new script and forget past requests, keeping the same port."""
        with self._lock:
            self.script, self._calls = (script if callable(script) else load_script(script)), 0
            self.recorded.clear()

    @property
    def request_bodies(self) -> list[dict]:
        return [r["body"] for r in self.recorded]

    # -- internals ---------------------------------------------------------------------------

    def _next(self, body: dict, headers, path: str) -> tuple[int, dict, dict]:
        with self._lock:
            self._calls += 1
            n = self._calls
            entry = None if callable(self.script) else (self.script[n - 1] if n <= len(self.script) else None)
            status, payload, extra_headers = self._respond(n, entry, body)
            record = {"call": n, "ts": round(time.time(), 3), "path": path, "headers": redact_headers(headers),
                      "status": status, "body": body}
            self.recorded.append(record)
            if self.record_path:
                with self.record_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
        return status, payload, extra_headers

    def _respond(self, n: int, entry: dict | None, body: dict) -> tuple[int, dict, dict]:
        if callable(self.script):  # a function (n, request body) -> entry, for stand-in models that react to the conversation
            entry = self.script(n, body)
        if entry is None:
            return 500, _error(f"mock script exhausted at call {n}"), {}
        status = int(entry.get("status", 200))
        if status != 200:
            return status, _error(entry.get("message", f"scripted error {status}")), dict(entry.get("headers", {}))

        message: dict = {"role": "assistant", "content": entry.get("content")}
        calls = entry.get("tool_calls") or []
        if calls:
            message["tool_calls"] = [
                {"id": f"call_{n}_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"] if isinstance(c["arguments"], str) else json.dumps(c.get("arguments", {}))}}
                for i, c in enumerate(calls)
            ]
        usage = entry.get("usage") or {
            "prompt_tokens": _tokens(json.dumps(body.get("messages", [])) + json.dumps(body.get("tools", []))),
            "completion_tokens": _tokens(json.dumps(message)),
        }
        usage = dict(usage)
        if "cached_tokens" in usage:  # report it the way OpenAI does
            usage["prompt_tokens_details"] = {"cached_tokens": usage.pop("cached_tokens")}
        payload = {
            "id": f"mock-{n}", "object": "chat.completion", "created": int(time.time()),
            "model": entry.get("model", "mock-small"),
            "choices": [{"index": 0, "message": message,
                         "finish_reason": entry.get("finish_reason", "tool_calls" if calls else "stop")}],
            "usage": usage,
        }
        if entry.get("provider"):
            payload["provider"] = entry["provider"]
        if entry.get("extra_fields"):  # e.g. a Bifrost-style {"routing_info": {...}}
            payload["extra_fields"] = entry["extra_fields"]
        return 200, payload, dict(entry.get("response_headers", {}))  # e.g. {"x-bifrost-cache-hit": "true"}

    def _make_handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _send(self, status: int, payload: dict, headers: dict | None = None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, str(v))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, {"object": "list", "data": [{"id": "mock-small", "object": "model"}]})
                else:
                    self._send(200, {"status": "ok"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, _error("request body is not JSON"))
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._send(404, _error(f"no such path {self.path}"))
                self._send(*provider._next(body, self.headers, self.path))

        return Handler


class MockTavily:
    """A tiny stand-in for Tavily's POST /search. Records requests; answers with scripted results and credits."""

    def __init__(self, results: list[dict] | None = None, status: int = 200, credits: int = 1, port: int = 0):
        self.results = results if results is not None else [
            {"title": "Docs", "url": "https://docs.example.com/a", "content": "Use the new API. " * 40, "score": 0.9}]
        self.status, self.credits = status, credits
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append({"path": self.path, "authorization": self.headers.get("Authorization", ""), "body": body})
                payload = json.dumps({"query": body.get("query"), "results": outer.results, "usage": {"credits": outer.credits},
                                      "response_time": 0.1} if outer.status == 200 else {"detail": {"error": "scripted"}}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> "MockTavily":
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def _error(message: str) -> dict:
    return {"error": {"message": message, "type": "mock_error"}}


def _tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pinch-mock", description="Scripted OpenAI-compatible mock provider.")
    parser.add_argument("script", help="JSON or YAML script of responses")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--record", default="mock-requests.jsonl", help="file that every request is appended to")
    args = parser.parse_args()
    provider = MockProvider(args.script, args.record, port=args.port)
    print(f"pinch-mock listening on {provider.url} (recording requests to {args.record}). Ctrl-C to stop.")
    try:
        provider._server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
