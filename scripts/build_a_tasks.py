"""Build set A (bench/tasks/A) from the Aider polyglot benchmark: its Python exercises.

    git clone https://github.com/Aider-AI/polyglot-benchmark /path/to/polyglot
    git -C /path/to/polyglot checkout 7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f
    uv run python scripts/build_a_tasks.py /path/to/polyglot

Per exercise: repo/ is the folder an agent starts with (stub + tests, without .meta and .docs, so the reference solution
`.meta/example.py` is not in sight), check/ is the pristine non-solution files, gold/ is the reference solution under the
solution file's name. The prompt follows the one Aider's own harness sends (the exercise's instructions, then a short tail).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

COMMIT = "7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f"
REPO_URL = "https://github.com/Aider-AI/polyglot-benchmark"
OUT = Path(__file__).resolve().parents[1] / "bench" / "tasks" / "A"
PYTEST = "pytest==8.4.2"

SOURCE_MD = """# Set A: Aider polyglot benchmark, Python exercises

- What: the 34 Python exercises of [Aider-AI/polyglot-benchmark](https://github.com/Aider-AI/polyglot-benchmark)
- Pinned commit: `7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f` (2024-12-22)
- Origin and licence: the exercises are taken from the [Exercism Python track](https://github.com/exercism/python).
  Content is copyright (c) Exercism and released under the MIT licence (`LICENSE-exercism-python.txt`, copied from
  https://github.com/exercism/python/blob/main/LICENSE). The polyglot-benchmark repository has no licence file of its own; its README says
  the exercises "are used in accordance with Exercism's open source licenses" and credits Exercism. We do the same.
- Pass means: the exercise's own test file, restored to its pristine content after the agent has finished, passes under pytest
  in a fresh container.
- What we changed: nothing in the exercises. `repo/` is the exercise folder without `.meta/` and `.docs/` (the reference solution
  `.meta/example.py` is stored only in `gold/`, which no agent sees), and the prompt is the exercise's instructions plus a short tail.
- Built by `scripts/build_a_tasks.py`.
"""
LICENSE_TEXT = 'MIT License\n\nCopyright (c) 2021 Exercism\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\nof this software and associated documentation files (the "Software"), to deal\nin the Software without restriction, including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.\n'

TAIL = """

####

Use the above instructions to modify the supplied file{s}: {files}
Don't change the names of existing functions or classes, as they may be called by hidden tests.
Only use the Python standard library. The tests are in {tests}; you can run them with: python -m pytest -q {tests}
Do not edit the test file{s2}."""


def main(polyglot: Path) -> None:
    head = subprocess.run(["git", "-C", str(polyglot), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != COMMIT:
        raise SystemExit(f"polyglot-benchmark is at {head}, expected {COMMIT}. Check out the pinned commit first.")
    practice = polyglot / "python" / "exercises" / "practice"
    OUT.mkdir(parents=True, exist_ok=True)
    for old in [p for p in OUT.iterdir() if p.is_dir()]:
        shutil.rmtree(old)
    (OUT / "SOURCE.md").write_text(SOURCE_MD)
    (OUT / "LICENSE-exercism-python.txt").write_text(LICENSE_TEXT)
    count = 0
    for ex in sorted(p for p in practice.iterdir() if p.is_dir()):
        cfg = json.loads((ex / ".meta" / "config.json").read_text())
        solution, tests = cfg["files"]["solution"], cfg["files"]["test"]
        examples = cfg["files"].get("example", [])
        if len(examples) != len(solution):
            raise SystemExit(f"{ex.name}: {len(examples)} example files for {len(solution)} solution files")
        instructions = (ex / ".docs" / "instructions.md").read_text().strip()
        append = ex / ".docs" / "instructions.append.md"
        if append.exists():
            instructions += "\n\n" + append.read_text().strip()
        task = OUT / ex.name
        repo, check, gold = task / "repo", task / "check", task / "gold"
        for d in (repo, check, gold):
            d.mkdir(parents=True)
        for f in sorted(p for p in ex.rglob("*") if p.is_file() and ".meta" not in p.relative_to(ex).parts
                        and ".docs" not in p.relative_to(ex).parts):
            rel = f.relative_to(ex)
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, repo / rel)
            if str(rel) not in solution:  # everything that is not the solution is restored before checking
                (check / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, check / rel)
        for src, dst in zip(examples, solution):
            (gold / dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ex / src, gold / dst)
        files = ", ".join(solution)
        prompt = instructions + TAIL.format(s="s" if len(solution) > 1 else "", files=files, tests=" ".join(tests),
                                            s2="s" if len(tests) > 1 else "")
        (task / "task.json").write_text(json.dumps({
            "id": f"A/{ex.name}", "set": "A", "title": f"Exercism Python exercise: {ex.name}", "prompt": prompt,
            "authored_by": "exercism (via Aider polyglot-benchmark)",
            "env": {"pip": [PYTEST], "pythonpath": ["."]},
            "check": {"command": "python -m pytest -q -p no:cacheprovider " + " ".join(tests), "timeout": 300},
            "solution_files": solution,
            "test_command": "python -m pytest -q " + " ".join(tests),  # the command the prompt tells the agent; pinch's stop-on-green uses it too
            "source": {"benchmark": "Aider-AI/polyglot-benchmark", "url": REPO_URL, "commit": COMMIT,
                       "path": f"python/exercises/practice/{ex.name}", "license": "MIT",
                       "attribution": "Exercism python track exercise (c) Exercism, MIT licence; see ../LICENSE-exercism-python.txt"},
        }, indent=2) + "\n")
        count += 1
    print(f"wrote {count} tasks to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
