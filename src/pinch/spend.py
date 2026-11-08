"""Per-user cumulative spend file, for the budget check when Bifrost cannot report the remaining budget.

~/.local/share/pinch/spend/<user>.json holds {"period": "2026-11", "usd": 0.0123}. The period is the calendar month, to
match the gateway's monthly budgets: a new month starts from zero. Written after every model call.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path


class SpendFile:
    def __init__(self, directory: Path, user: str):
        self.path = Path(directory) / (re.sub(r"[^A-Za-z0-9_.-]", "_", user or "default") + ".json")

    def total(self) -> float:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0.0
        return float(data.get("usd", 0.0)) if data.get("period") == time.strftime("%Y-%m") else 0.0

    def add(self, usd: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"period": time.strftime("%Y-%m"), "usd": round(self.total() + usd, 8)}), encoding="utf-8")
        tmp.replace(self.path)
