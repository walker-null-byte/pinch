"""Tools: the coding-tools registry (37 tools, built on TurboMCP), loaded in-process.

No MCP transport is involved. We import coding-tools' server file, which fills a TurboMCP
registry, and call the registry directly. Two modes decide what the model is shown:

  full  every tool schema is sent on every request
  lazy  only TurboMCP's 3 meta-tools are sent: search_tools, get_tool, call_tool

pinch itself runs every tool, so the approval gate can look at the real tool name even when
the model reaches it through `call_tool` (see `parse_call`).

Second layer of defence: coding-tools has its own guardrails. We set CODING_MCP_ROOT to the
repo so its file tools are jailed there, whatever the gate decides.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .approval import META_CALL
from .tavily import DESCRIPTION as DOCS_DESCRIPTION

VENDORED = Path(__file__).parent / "vendor" / "coding_mcp_server.py"
# Registered by coding-tools / TurboMCP but not wanted by an agent. Web tools are off in Phase 1
# (they would leak a confidential repo's queries); restart_server would re-exec pinch itself.
REMOVED_TOOLS = ("internet_search", "webpage_scrape", "restart_server")
MAX_RESULT_CHARS = 12_000
DOCS_DOC = """Look up documentation or an error message on the web.

Args:
    query (str): One line of error text and library names. No source code, no paths, no secrets.
"""


@dataclass
class ToolCall:
    """A tool call after unwrapping `call_tool`. This is what the gate and the executor both see."""
    name: str
    args: dict
    via_call_tool: bool = False
    error: str | None = None  # set when the call is malformed; nothing is run


# Technique `smalledits`: these descriptions steer the model to send only the change, not the whole file.
SMALL_EDIT_DESCRIPTIONS = {
    "edit_file": "Preferred way to change an existing file: replace one exact piece of text with another. Sends only the change.",
    "apply_patch": "Change an existing file with a unified diff. Use it for several changes in one file. Sends only the changes.",
    "write_file": "Create a NEW file. Do not use it on a file that already exists (that resends the whole file): use edit_file or apply_patch.",
}


class ToolBox:
    def __init__(self, repo: Path, mode: str = "lazy", tools_file: Path | None = None, small_edits: bool = False,
                 docs=None):
        self.repo = Path(os.path.realpath(repo))
        self.mode = mode
        self.registry = _load_registry(self.repo, tools_file or Path(os.environ.get("PINCH_CODING_TOOLS", VENDORED)))
        for name in REMOVED_TOOLS:
            self.registry.unregister(name)
        if small_edits:
            for name, text in SMALL_EDIT_DESCRIPTIONS.items():
                self.registry.tools[name]["description"] = text
        if docs is not None and docs.enabled:  # Tavily docs_lookup: never registered when it is off (e.g. a confidential repo)
            def docs_lookup(query: str):
                return docs(query)
            docs_lookup.__doc__ = DOCS_DOC
            self.registry.register(docs_lookup, name="docs_lookup", description=DOCS_DESCRIPTION)
        try:
            from turbomcp.mcp_stdio import _LAZY_TOOLS  # TurboMCP keeps its meta-tool schemas here
        except ImportError as e:
            raise SystemExit(f"pinch: this turbomcp has no lazy meta-tools ({e}). Install turbomcp>=0.3.0.")
        self._meta = {t["name"]: t for t in _LAZY_TOOLS}
        self.only: frozenset[str] | None = None  # when set, the model is offered (and may call) just these tools, in full mode

    def restrict(self, names) -> None:
        """Local tier: a small, direct tool set. The model sees only these schemas and may call only these tools."""
        known = set(self.registry.tools)
        self.only = frozenset(n for n in names if n in known)
        self.mode = "full"

    def offered(self, name: str) -> bool:
        return self.only is None or name in self.only

    def offered_schemas(self) -> dict[str, dict]:
        """{tool name: JSON-schema parameters} for exactly what `schemas()` sends. The text-call parser accepts nothing else."""
        return {t["function"]["name"]: t["function"].get("parameters") or {} for t in self.schemas()}

    # -- what the model is shown -------------------------------------------------------------

    def schemas(self) -> list[dict]:
        """Tool definitions in OpenAI function-calling format, for the current mode."""
        if self.mode == "full":
            tools = self.registry.to_openai_tools()
            return [t for t in tools if self.offered(t["function"]["name"])]
        return [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}}
            for t in self._meta.values()
        ]

    # -- what the model asked for ------------------------------------------------------------

    def parse_call(self, name: str, raw_args) -> ToolCall:
        """Turn a model tool call into the real call, unwrapping `call_tool` to its inner tool."""
        args, err = _as_dict(raw_args, "arguments")
        if err:
            return ToolCall(name, {}, error=err)
        via = False
        if name == META_CALL:
            inner = args.get("name")
            if not isinstance(inner, str):
                return ToolCall(name, args, error="call_tool needs a string 'name'.")
            inner_args, err = _as_dict(args.get("arguments"), "'arguments' of call_tool")
            if err:
                return ToolCall(inner, {}, True, error=err)
            name, args, via = inner, inner_args, True
        if name == "docs_lookup" and name not in self.registry.tools:
            return ToolCall(name, args, via, error="docs_lookup is switched off for this repo (confidential repo, or no Tavily key).")
        if name not in self._meta and name not in self.registry.tools:
            return ToolCall(name, args, via, error=f"Unknown tool '{name}'. Use search_tools to find one.")
        return ToolCall(name, args, via)

    # -- running it --------------------------------------------------------------------------

    def execute(self, call: ToolCall, cap: bool = True) -> str:
        """Run an already-approved call and return the result as compact text.

        With cap=False the text is not cut to MAX_RESULT_CHARS, so the agent can shrink it properly first (a cut would break
        the JSON that test-output compression reads); it then applies `cap_text` itself."""
        try:
            with _in_repo(self.repo), _no_bytecode():
                if call.name == "search_tools":
                    result = self.registry.search_tools(str(call.args.get("query", "")), top_n=int(call.args.get("top_n", 5)))
                elif call.name == "get_tool":
                    result = self.registry.get_capabilities(name=call.args.get("name"))
                elif call.name in self._meta:
                    result = {"error": "This meta-tool cannot be called through call_tool."}
                else:
                    result = self.registry.call(call.name, call.args)
        except Exception as e:  # tool errors go back to the model as text, not crashes
            return f"Error: {type(e).__name__}: {e}"
        return _render(result, cap)


def cap_text(text: str) -> str:
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n[output cut: {len(text) - MAX_RESULT_CHARS} more characters. Read less at a time.]"
    return text


def _as_dict(raw, label: str) -> tuple[dict, str | None]:
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            return {}, f"Could not parse {label} as JSON: {e}"
    if not isinstance(raw, dict):
        return {}, f"{label} must be a JSON object."
    return raw, None


def _render(result, cap: bool = True) -> str:
    text = result if isinstance(result, str) else json.dumps(result, separators=(",", ":"), default=str)
    return cap_text(text) if cap else text


@contextlib.contextmanager
def _no_bytecode():
    """Run commands with PYTHONDONTWRITEBYTECODE=1, so a test run never trusts a stale .pyc.

    Python decides a cached .pyc is fresh from the source file's modification time (whole seconds) and size.
    An agent that edits a file twice within one second, keeping its size, would otherwise run the OLD code.
    The variable is set only while pinch executes a tool, then put back, so it never leaks into the caller.
    """
    saved = os.environ.get("PYTHONDONTWRITEBYTECODE")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        else:
            os.environ["PYTHONDONTWRITEBYTECODE"] = saved


@contextlib.contextmanager
def _in_repo(repo: Path):
    """coding-tools resolves 'current directory' from the process cwd, so run inside the repo."""
    before = os.getcwd()
    os.chdir(repo)
    try:
        yield
    finally:
        os.chdir(before)


_loads = 0


def _load_registry(repo: Path, tools_file: Path):
    """Import coding-tools' server file with its jail set to `repo`, and return its registry.

    The file reads CODING_MCP_ROOT once at import time, so we set it just for the import.
    """
    global _loads
    if not tools_file.is_file():
        raise SystemExit(f"pinch: coding-tools not found at {tools_file}. Set PINCH_CODING_TOOLS or reinstall pinch.")
    _loads += 1
    spec = importlib.util.spec_from_file_location(f"pinch_coding_tools_{_loads}", tools_file)
    module = importlib.util.module_from_spec(spec)
    saved = os.environ.get("CODING_MCP_ROOT")
    os.environ["CODING_MCP_ROOT"] = str(repo)
    try:
        spec.loader.exec_module(module)
    except ImportError as e:
        raise SystemExit(f"pinch: could not load coding-tools ({e}). It needs turbomcp>=0.3.0.")
    finally:
        if saved is None:
            os.environ.pop("CODING_MCP_ROOT", None)
        else:
            os.environ["CODING_MCP_ROOT"] = saved
    return module.mcp
