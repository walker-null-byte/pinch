"""Phase 4a: the benchmark harness.

    uv run python -m pinch.bench setup                  build the sandbox tools (one image, a few venvs)
    uv run python -m pinch.bench list                   the task sets
    uv run python -m pinch.bench validate               every task: unsolved must fail, the reference solution must pass
    uv run python -m pinch.bench run --set A,B,C --modes ... --model <name> --repeat N --out bench/results/x.jsonl
    uv run python -m pinch.bench summarize bench/results/x.jsonl
    uv run python -m pinch.bench smoke                  offline end-to-end check with mock providers (every output says MOCK)
"""

from __future__ import annotations

import os
from pathlib import Path

# The checkout root (holds bench/). PINCH_BENCH_DIR points elsewhere if the package is installed away from the checkout.
ROOT = Path(os.environ.get("PINCH_BENCH_DIR") or Path(__file__).resolve().parents[3] / "bench")
TASKS_DIR = ROOT / "tasks"
RESULTS_DIR = ROOT / "results"
CACHE_DIR = ROOT / ".cache"
