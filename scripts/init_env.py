"""Fill the empty values in pinch/.env for the real-provider setup. Never prints a value.

    uv run python scripts/init_env.py            # fill what is empty, keep everything else
    uv run python scripts/init_env.py --check    # only report which names are set / empty

- Random values (virtual keys, admin password, setup token) are made with `secrets.token_urlsafe`; virtual keys get the `sk-bf-` prefix.
- A name that already has a value is never changed (so the provider keys stay as they are).
- Existing lines and comments stay where they are. An empty `NAME=` is filled in place, a commented `# NAME=` line is
  switched on, and a name that is missing is added under its section.
- The file is rewritten with mode 600. Only names and "set" / "kept" are printed.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"


def vk() -> str:
    return "sk-bf-" + secrets.token_urlsafe(24)


def token() -> str:
    return secrets.token_urlsafe(24)


# (name, value factory or fixed string, anchor: the name after which a missing line is added, comment placed above a new line)
PLAN: list[tuple[str, object, str, str]] = [
    ("PINCH_MODEL", "gemini/gemini-3.5-flash-lite", "PINCH_BASE_URL", ""),
    ("PINCH_GATEWAY_KEY", vk, "PINCH_MODEL", ""),
    ("PINCH_LOCAL_MODEL", "local/gemma4", "PINCH_MODEL",
     "# Model name sent for the local tier (confidential repos, and 'continue on the local tier'). Empty = same as PINCH_MODEL."),
    ("PINCH_CONFIDENTIAL_KEY", vk, "PINCH_KEY_HEADER", ""),
    ("PINCH_BUDGET_VK_ID", "vk-me", "PINCH_CONFIDENTIAL_VK_ID", ""),
    ("PINCH_GATEWAY_ADMIN_USER", "admin", "PINCH_BUDGET_VK_ID", ""),
    ("PINCH_GATEWAY_ADMIN_PASSWORD", token, "PINCH_GATEWAY_ADMIN_USER", ""),
    ("PINCH_GATEWAY_SETUP_TOKEN", token, "PINCH_GATEWAY_ADMIN_PASSWORD",
     "# One-time token the real gateway needs to switch on its admin login (gateway/config.real.json)."),
    ("PINCH_BENCH_KEY", vk, "PINCH_GATEWAY_SETUP_TOKEN",
     "# Gemini-only virtual key with a $15 budget and no fallback, for `pinch.bench run` (model never switches mid-benchmark)."),
    ("PINCH_DEMO_LOW_KEY", vk, "PINCH_BENCH_KEY", "# Demo keys: a $0.01 budget (shows the budget stop) and a key whose primary provider is down (shows failover)."),
    ("PINCH_FAILOVER_DEMO_KEY", vk, "PINCH_DEMO_LOW_KEY", ""),
    ("PINCH_SMALL_MODEL", "gemini/gemini-3.5-flash-lite", "TAVILY_API_KEY", ""),
    ("PINCH_BIG_MODEL", "gemini/gemini-3.6-flash", "PINCH_SMALL_MODEL", ""),
    ("LOCAL_PROVIDER_KEY", "lm-studio", "ANTHROPIC_API_KEY", ""),
]


def split(line: str) -> tuple[str, str] | None:
    """(name, value) of an active `NAME=value` line, else None."""
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    name, _, value = s.partition("=")
    return name.strip(), value.strip()


def commented(line: str, name: str) -> bool:
    s = line.strip()
    return s.startswith("#") and s.lstrip("# ").startswith(name + "=")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="only report which names are set, change nothing")
    p.add_argument("--file", type=Path, default=ENV)
    args = p.parse_args()
    path = args.file
    lines = path.read_text(encoding="utf-8").splitlines()

    def index_of(name: str) -> int | None:
        for i, line in enumerate(lines):
            parsed = split(line)
            if parsed and parsed[0] == name:
                return i
        return None

    report: list[str] = []
    for name, source, anchor, comment in PLAN:
        value = source() if callable(source) else source
        i = index_of(name)
        if i is not None:
            if split(lines[i])[1]:
                report.append(f"{name}: kept (already set)")
            elif args.check:
                report.append(f"{name}: EMPTY")
            else:
                lines[i] = f"{name}={value}"
                report.append(f"{name}: set")
            continue
        if args.check:
            report.append(f"{name}: MISSING")
            continue
        new = [f"{name}={value}"]
        if comment:
            new.insert(0, comment)
        for j, line in enumerate(lines):  # a commented-out line for this name: switch it on in place
            if commented(line, name):
                lines[j] = new[-1]
                break
        else:
            at = index_of(anchor)
            at = len(lines) - 1 if at is None else at
            lines[at + 1:at + 1] = new
        report.append(f"{name}: set")

    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "TAVILY_API_KEY"):
        i = index_of(name)
        report.append(f"{name}: " + ("set (untouched)" if i is not None and split(lines[i])[1] else "EMPTY"))
    if not args.check:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
