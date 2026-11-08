"""Measure what Bifrost Code Mode puts into the prompt for the coding-tools set, and compare it with pinch's modes.

We do NOT run tasks through Code Mode. Bifrost would execute the tools inside the gateway, which bypasses pinch's
approval gate (the gate runs in pinch, before execution). This script only looks at the request Bifrost sends on to
a (mock) provider: it exposes the same 34 coding-tools over a tiny MCP-over-HTTP shim, registers that as an MCP client
in a throwaway Bifrost, sends one chat request, and reads the request body the mock provider received.

    uv run python scripts/measure_code_mode.py

Needs Docker and the pinned Bifrost image. Offline otherwise. Sizes are bytes of JSON (and bytes/4 as a rough token
count), not provider-reported tokens: the mock provider does the counting.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from pinch import prove
from pinch.mock_provider import MockProvider
from pinch.tools import ToolBox

MCP_PORT = 8200
VK = prove.DEMO_ENV["PINCH_VK_ALICE"]


def mcp_shim(registry) -> ThreadingHTTPServer:
    """Just enough MCP streamable-HTTP (JSON-RPC over POST) to expose a TurboMCP registry to Bifrost."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, payload, status=200):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_DELETE(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            method, mid = msg.get("method"), msg.get("id")
            if mid is None:  # a notification
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif method == "initialize":
                self._json({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "coding-tools", "version": "0.3.0"}}})
            elif method == "tools/list":
                self._json({"jsonrpc": "2.0", "id": mid, "result": {"tools": registry.to_mcp_tools()}})
            elif method == "ping":
                self._json({"jsonrpc": "2.0", "id": mid, "result": {}})
            else:
                self._json({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "not supported in this shim"}})

    server = ThreadingHTTPServer(("127.0.0.1", MCP_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, kwargs={"poll_interval": 0.05}).start()
    return server


def gateway_config(code_mode: bool) -> dict:
    config = json.loads((prove.GATEWAY_DIR / "config.json").read_text())
    config["mcp"] = {"client_configs": [{
        "name": "coding_tools", "connection_type": "http", "connection_string": f"http://127.0.0.1:{MCP_PORT}/mcp", "auth_type": "none",
        "tools_to_execute": ["*"], "is_code_mode_client": code_mode, "allow_on_all_virtual_keys": True, "is_ping_available": False}]}
    return config


def injected(mock: MockProvider, code_mode: bool, workdir: Path) -> dict:
    path = workdir / ("codemode.json" if code_mode else "classic.json")
    path.write_text(json.dumps(gateway_config(code_mode)))
    prove.start_gateway(path)
    try:
        mock.reset([{"content": "ok", "times": 3}])
        question = [{"role": "user", "content": "hello"}]
        reply = httpx.post(prove.GATEWAY_URL + "/v1/chat/completions", headers={"x-bf-vk": VK},
                           json={"model": "mock-small", "messages": question, "max_tokens": 50}, timeout=30)
        reply.raise_for_status()
        body = mock.recorded[0]["body"]
    finally:
        prove.stop_gateway()
    tools = body.get("tools") or []
    return {"tools": len(tools), "names": [t["function"]["name"] for t in tools], "tool_bytes": len(json.dumps(tools, separators=(",", ":"))),
            "message_bytes_added": len(json.dumps(body["messages"])) - len(json.dumps(question))}


def main() -> int:
    if not prove.docker_ok():
        print("Docker is not available, so Code Mode cannot be measured. No number is reported.")
        return 2
    toolbox = {m: ToolBox(Path(tempfile.mkdtemp()), m) for m in ("full", "lazy")}
    rows = {f"pinch {m}": toolbox[m].schemas() for m in toolbox}
    results = {name: {"tools": len(s), "tool_bytes": len(json.dumps(s, separators=(",", ":"))), "message_bytes_added": 0,
                      "names": [t["function"]["name"] for t in s]} for name, s in rows.items()}
    shim = mcp_shim(toolbox["full"].registry)
    mock = MockProvider([], port=8101).start()
    other = [MockProvider([], port=port).start() for port in (8102, 8103)]
    workdir = Path(tempfile.mkdtemp(prefix="pinch-codemode-"))
    try:
        results["bifrost MCP, classic"] = injected(mock, False, workdir)
        results["bifrost Code Mode"] = injected(mock, True, workdir)
    except Exception as e:
        print(f"Code Mode could not be measured offline: {type(e).__name__}: {e}")
        return 2
    finally:
        shim.shutdown()
        mock.stop()
        for m in other:
            m.stop()
    print("What each mode puts into every request for the 34 coding-tools (offline byte estimate; tokens ~ bytes/4):\n")
    print(f"{'mode':<24}{'tools sent':>11}{'schema bytes':>14}{'+ message bytes':>17}{'~tokens':>9}")
    for name, r in results.items():
        total = r["tool_bytes"] + r["message_bytes_added"]
        print(f"{name:<24}{r['tools']:>11}{r['tool_bytes']:>14}{r['message_bytes_added']:>17}{total // 4:>9}")
    print("\nTools shown in Code Mode:", ", ".join(results["bifrost Code Mode"]["names"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
