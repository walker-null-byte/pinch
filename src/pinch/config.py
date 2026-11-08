"""Configuration: defaults < ~/.config/pinch/config.toml < environment < command-line flags.

Secrets (the gateway key) come from the environment only, never from config.toml.
A `.env` file is loaded from a few trusted places. It is never loaded from the repo
being worked on, because an untrusted repo could use it to redirect the model traffic.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

STYLES = ("normal", "brief", "tiny", "caveman")
MODES = ("full", "lazy")


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pinch"


def default_config_path() -> Path:
    return config_dir() / "config.toml"


def default_session_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "pinch" / "sessions"


@dataclass
class Config:
    # model endpoint (Phase 2 points base_url at the Bifrost gateway)
    base_url: str = "http://127.0.0.1:8089/v1"
    model: str = "mock-small"
    gateway_key: str = ""  # the user's virtual key; from env only; never printed
    key_header: str = "x-bf-vk"  # Bifrost's virtual-key header (docs.getbifrost.ai/features/governance/virtual-keys)
    # agent behaviour
    mode: str = "lazy"  # "full" sends every tool schema; "lazy" sends only the 3 meta-tools
    style: str = "brief"
    max_steps: int = 30
    max_tokens: int = 2048
    auto_approve: bool = False  # set only by the --auto-approve flag, never from a file
    allow_tools: tuple[str, ...] = ()  # tools the user pre-approves in config (e.g. git_status)
    # policy
    confidential_repos: tuple[str, ...] = ()
    # files
    session_dir: Path = field(default_factory=default_session_dir)
    config_path: Path = field(default_factory=default_config_path)
    # confidential repos: a virtual key that can reach only local providers (from env only)
    confidential_key: str = ""
    confidential_vk_id: str = "vk-confidential"  # its id in Bifrost, used to verify it at startup
    local_providers: tuple[str, ...] = ("local",)  # Bifrost provider names that run on this machine / network
    # Model name sent when the local tier is used (a confidential repo, or "continue on the local tier"). Empty = keep `model`.
    # With real providers `model` names a cloud model that the local-only key may not use, so this is needed (e.g. "local/gemma4").
    local_model: str = ""
    # Tools offered on the local tier (used when local_model is set): a small direct set in full mode, so a small model does not
    # have to drive search_tools/call_tool.
    local_tools: tuple[str, ...] = ("read_file", "list_directory", "grep_search", "edit_file", "write_file", "run_test", "run_command")
    # gateway admin API (read-only use: budgets and key checks). Credentials from env only.
    admin_url: str = ""  # default: base_url without the trailing /v1
    admin_user: str = ""
    admin_password: str = ""
    # budget (requirement 2)
    budget_vk_id: str = ""  # the user's virtual key id in Bifrost, to read the remaining budget
    budget_usd: float | None = None  # fallback limit for pinch's own ledger when Bifrost can't tell us
    # reliability: tries per model call when the gateway is unreachable or answers 429/5xx
    retries: int = 3
    retry_backoff: float = 1.0  # seconds; doubles each retry
    # Bifrost cache namespace. With the `cache` technique on, pinch sends x-bf-cache-key = this, or the session id
    # (a fresh namespace per run) when empty. Never sent for a confidential repo.
    cache_key: str = ""
    # token-saving techniques: "all", "none", or a comma list (see savings.TECHNIQUES). Each is switchable on its own.
    savings: str = "all"
    trim_after: int = 4  # history trimming: tool output older than this many model steps becomes a one-line stub
    test_command: str = ""  # stop-on-green runs this; empty = let coding-tools auto-detect the repo's test command
    # cascade: start on the small tier, escalate to the big tier on clear failure signals. Empty = off.
    small_model: str = ""
    big_model: str = ""
    escalate_after_failed_tests: int = 2  # tests still failing after this many edit-and-test rounds
    escalate_after_tool_errors: int = 3  # this many tool errors in a row
    # Tavily docs_lookup (needs TAVILY_API_KEY). Off for confidential repos.
    tavily_key: str = ""  # from env only
    tavily_url: str = "https://api.tavily.com"
    # per-user cumulative spend file, for the ledger fallback of the budget check
    spend_dir: Path = field(default_factory=lambda: Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "pinch" / "spend")


def load_env_file(path: Path) -> None:
    """Tiny .env reader: KEY=VALUE lines. Existing environment variables win."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


def load_trusted_env_files() -> None:
    explicit = os.environ.get("PINCH_ENV_FILE")
    if explicit:
        load_env_file(Path(explicit))
    load_env_file(config_dir() / ".env")
    source_root = Path(__file__).resolve().parents[2]  # the pinch checkout, when run from source
    if (source_root / "pyproject.toml").exists():
        load_env_file(source_root / ".env")


def load_config(config_path: Path | None = None, overrides: dict | None = None) -> Config:
    """Build the Config. `overrides` holds command-line values; None means 'not given'."""
    load_trusted_env_files()
    cfg = Config()
    if config_path is not None:
        cfg.config_path = config_path
    elif os.environ.get("PINCH_CONFIG"):
        cfg.config_path = Path(os.environ["PINCH_CONFIG"])

    try:
        data = tomllib.loads(cfg.config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"pinch: cannot read {cfg.config_path}: {e}")

    for key in ("base_url", "model", "key_header", "mode", "style", "max_steps", "max_tokens", "budget_usd", "budget_vk_id",
                "confidential_vk_id", "admin_url", "retries", "retry_backoff", "savings", "trim_after", "test_command",
                "small_model", "big_model", "local_model", "escalate_after_failed_tests", "escalate_after_tool_errors", "tavily_url", "cache_key"):
        if key in data:
            setattr(cfg, key, data[key])
    cfg.allow_tools = tuple(data.get("allow_tools", ()))
    cfg.confidential_repos = tuple(data.get("confidential_repos", ()))
    cfg.local_providers = tuple(data.get("local_providers", cfg.local_providers))
    cfg.local_tools = tuple(data.get("local_tools", cfg.local_tools))
    if "session_dir" in data:
        cfg.session_dir = Path(data["session_dir"]).expanduser()

    env = os.environ
    for key, name, cast in (
        ("base_url", "PINCH_BASE_URL", str),
        ("model", "PINCH_MODEL", str),
        ("key_header", "PINCH_KEY_HEADER", str),
        ("mode", "PINCH_MODE", str),
        ("style", "PINCH_STYLE", str),
        ("max_steps", "PINCH_MAX_STEPS", int),
        ("max_tokens", "PINCH_MAX_TOKENS", int),
    ):
        if env.get(name):
            setattr(cfg, key, cast(env[name]))
    if env.get("PINCH_SESSION_DIR"):
        cfg.session_dir = Path(env["PINCH_SESSION_DIR"]).expanduser()
    cfg.gateway_key = env.get("PINCH_GATEWAY_KEY", "")
    cfg.confidential_key = env.get("PINCH_CONFIDENTIAL_KEY", "")
    cfg.admin_user = env.get("PINCH_GATEWAY_ADMIN_USER", "")
    cfg.admin_password = env.get("PINCH_GATEWAY_ADMIN_PASSWORD", "")
    cfg.tavily_key = env.get("TAVILY_API_KEY", "")
    for key, name in (("confidential_vk_id", "PINCH_CONFIDENTIAL_VK_ID"), ("budget_vk_id", "PINCH_BUDGET_VK_ID"),
                      ("admin_url", "PINCH_ADMIN_URL"), ("cache_key", "PINCH_CACHE_KEY"), ("savings", "PINCH_SAVINGS"),
                      ("test_command", "PINCH_TEST_COMMAND"), ("small_model", "PINCH_SMALL_MODEL"), ("big_model", "PINCH_BIG_MODEL"), ("local_model", "PINCH_LOCAL_MODEL"),
                      ("tavily_url", "PINCH_TAVILY_URL")):
        if env.get(name):
            setattr(cfg, key, env[name])
    if env.get("PINCH_LOCAL_TOOLS"):
        cfg.local_tools = tuple(t.strip() for t in env["PINCH_LOCAL_TOOLS"].split(",") if t.strip())
    if env.get("PINCH_BUDGET_USD"):
        cfg.budget_usd = float(env["PINCH_BUDGET_USD"])

    for key, value in (overrides or {}).items():
        if value is not None:
            setattr(cfg, key, value)

    from .savings import parse_savings  # fail early on a typo in the technique list
    try:
        parse_savings(cfg.savings)
    except ValueError as e:
        raise SystemExit(f"pinch: {e}")
    if not cfg.admin_url:
        cfg.admin_url = cfg.base_url.rstrip("/").removesuffix("/v1")
    if cfg.style not in STYLES:
        raise SystemExit(f"pinch: unknown style '{cfg.style}'. Choose one of: {', '.join(STYLES)}")
    if cfg.mode not in MODES:
        raise SystemExit(f"pinch: unknown tool mode '{cfg.mode}'. Choose one of: {', '.join(MODES)}")
    return cfg
