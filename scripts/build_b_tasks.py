"""Build set B (bench/tasks/B): real bug fixes from permissively licensed, pure-Python projects.

    # clone each repo from the SPECS table below into one folder, named as in `dir`
    uv run python scripts/build_b_tasks.py /path/to/clones

For each fix commit F with parent P:
  repo/   the project exactly as it was at P (the bug is present; this is the same tree as "F with its source change reverted"),
          minus documentation and images. The upstream LICENSE file is kept.
  check/  the test files F added or changed, as they are at F. The agent never sees them; they are laid over the work dir after
          the agent finishes, and the check command runs them.
  gold/   the non-test files F changed, as they are at F (the upstream fix). Used only to validate the task and by the MOCK model.
The prompts are written by us from the upstream commit message and tests: they describe the symptom and the expected behaviour.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "bench" / "tasks" / "B"
PYTEST8 = "pytest==8.4.2"
PYTEST9 = "pytest==9.2.0"
SKIP_DIRS = {"docs", "doc", ".github", "examples", "benchmarks", "scripts"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".lock", ".mo", ".po", ".pot"}
MAX_BYTES = 300_000

SPECS = [
    dict(
        slug="boltons-backoff-constant-factor", dir="boltons", url="https://github.com/mahmoud/boltons", license="BSD-3-Clause",
        fix="ead236e278ca0466bf468de746b5960fb12d7e5b", pip=[PYTEST8], pythonpath=["."],
        title="boltons: backoff() with factor=1.0 and no count crashes",
        run="python -m pytest -q tests/test_iterutils.py",
        prompt="""In boltons/iterutils.py, `backoff_iter` (and `backoff`, which wraps it) crashes when `factor=1.0` is used without an explicit `count`:

    >>> from boltons.iterutils import backoff
    >>> backoff(1, 10, factor=1.0)
    ZeroDivisionError: float division by zero      # raised by math.log(..., 1.0)

A factor of 1.0 means a constant delay. Make this behave sensibly:
- With an explicit count it already works and must keep working: `backoff(1, 10, count=5, factor=1.0) == [1.0, 1.0, 1.0, 1.0, 1.0]` and `backoff(2, 10, count=3, factor=1.0) == [2.0, 2.0, 2.0]`.
- When start == stop and there is no count, a constant factor needs no growth, so the result is a single value: `backoff(5, 5, factor=1.0) == [5.0]`.
- When start != stop and there is no count, the count cannot be inferred from a factor that never grows, so raise a `ValueError` with a clear message. That applies to `backoff(1, 10, factor=1.0)` and `backoff(0, 10, factor=1.0)`.

Existing behaviour for other factors must not change. Run the existing tests with: python -m pytest -q tests/test_iterutils.py""",
    ),
    dict(
        slug="humanize-intword-rounding-carry", dir="humanize", url="https://github.com/python-humanize/humanize", license="MIT",
        fix="ca892b368a535a79cc5e2181d052245b94fd6652", pip=[PYTEST9, "freezegun"], pythonpath=["src"],
        title="humanize: intword() does not carry to the next unit for very large numbers",
        run="python -m pytest -q tests/test_number.py",
        # humanize writes this file at build time (hatch-vcs); without an install it is missing, so we add the generated file
        extra_files={"src/humanize/_version.py": '__version__ = "0.0.0+bench"\n'},
        prompt="""`humanize.intword` renders a number that rounds up to the next unit incorrectly when the number is very large (above roughly 10**22):

    >>> import humanize
    >>> humanize.intword(10**6 - 1)
    '1.0 million'                 # correct: 999999 rounds up and carries to the next unit
    >>> humanize.intword(10**24 - 1)
    '1000.0 sextillion'           # wrong, should be '1.0 septillion'

The same carry must happen with a custom format, e.g. `humanize.intword(10**24 - 1, "%.0f") == "1 septillion"`, and for every unit boundary from million up to (and including) decillion, i.e. for values 10**e - 1 with e in 6, 9, ..., 33 the result is `1.0 <next unit>`. The mantissa must never render as 1000 or more below a decillion.

The gap beyond decillion is documented and must stay unchanged: `humanize.intword(10**36) == "1000.0 decillion"` and `humanize.intword(2 * 10**100) == "2.0 googol"`.

The code is in src/humanize/number.py. Run the existing tests with: python -m pytest -q tests/test_number.py""",
    ),
    dict(
        slug="more-itertools-bucket-falsey-validator", dir="more-itertools", url="https://github.com/more-itertools/more-itertools",
        license="MIT", fix="67fcf640477c78605580f5ee703a54c73c1e7a8a", pip=[PYTEST8], pythonpath=["."],
        title="more-itertools: bucket() ignores a validator that is falsey",
        run="python -m pytest -q tests/test_more.py -k Bucket",
        prompt="""`more_itertools.bucket` silently ignores its `validator` argument when the validator object is falsey (for example an empty `set` subclass that defines `__call__`):

    >>> import more_itertools as mi
    >>> class AllowedKeys(set):
    ...     def __call__(self, key):
    ...         return key in self
    >>> allowed = AllowedKeys()          # empty, so bool(allowed) is False
    >>> D = mi.bucket(iter([10, 20, 11, 21]), key=lambda x: x, validator=allowed)
    >>> list(D[10])
    [10]                                 # wrong: the validator rejects every key, so this should be []
    >>> 10 in D
    True                                 # wrong: should be False
    >>> set(D)
    {10, 20, 11, 21}                     # wrong: should be set()

Any validator that was passed (anything that is not None) must be used. Calling `bucket` without a validator must behave as before.

The code is in more_itertools/more.py. Run the existing tests with: python -m pytest -q tests/test_more.py -k Bucket""",
    ),
    dict(
        slug="cachetools-tlru-overwrite-expired", dir="cachetools", url="https://github.com/tkem/cachetools", license="MIT",
        fix="c0fdf6abab38040947d6fe2e38c507401d5e2350", pip=[PYTEST8], pythonpath=["src"],
        title="cachetools: TLRUCache keeps a stale value when overwritten with an already-expired one",
        run="python -m pytest -q tests/test_tlru.py",
        prompt="""A `cachetools.TLRUCache` silently keeps the OLD value when an existing key is overwritten with a value whose time-to-use is already over. `cache[key] = value` must either store the value or leave the key absent; it must never leave the previous value in place.

    from cachetools import TLRUCache

    def ttu(_key, value, now):
        return now + value               # a value of 0 is expired the moment it is stored

    cache = TLRUCache(maxsize=2, ttu=ttu, timer=some_timer)
    cache[1] = 5                         # stored; cache[1] == 5, len(cache) == 1, cache.currsize == 1
    cache[1] = 0                         # expired on arrival
    cache[1]                             # currently still returns 5; the key should be gone

After `cache[1] = 0` the key must be removed: `1 not in cache`, `cache.get(1) is None`, `len(cache) == 0`, `cache.currsize == 0` and `set(cache) == set()`.

The code is in src/cachetools/__init__.py. Run the existing tests with: python -m pytest -q tests/test_tlru.py""",
    ),
    dict(
        slug="parse-tt-matches-empty", dir="parse", url="https://github.com/r1chardj0n3s/parse", license="MIT",
        fix="52bf0b3862c6e21271e9582a3a98915247228763", pip=[PYTEST8], pythonpath=["."],
        title="parse: the {:tt} time type matches an empty string",
        run="python -m pytest -q -o addopts= tests/test_bugs.py tests/test_parse.py",
        prompt="""In the `parse` library the `tt` format type (a time of day) can match an empty string, which gives a bogus result and an endless loop:

    >>> import parse
    >>> parse.parse("{:tt}", "")
    <Result (datetime.time(0, 0),) {}>            # wrong: there is no time in the text, should be None
    >>> parse.parse("Meet at {:tt}", "Meet at ")   # should be None as well
    >>> parse.search("{:tt}", "there is no time here")   # wrongly reports a match at position 0, should be None
    >>> list(parse.findall("{:tt}", "no time"))   # never terminates; it should be []

Only the AM/PM and timezone parts of `tt` are optional. The time itself is required. Valid input must keep working:
`parse.parse("{:tt}", "10:21:36 PM")[0] == datetime.time(22, 21, 36)` and `parse.parse("Meet at {:tt}", "Meet at 10:21")[0] == datetime.time(10, 21)`.

The code is in parse/__init__.py. Run the existing tests with: python -m pytest -q -o addopts= tests/test_bugs.py tests/test_parse.py""",
    ),
    dict(
        slug="python-dotenv-empty-value-inline-comment", dir="python-dotenv", url="https://github.com/theskumar/python-dotenv",
        license="BSD-3-Clause", fix="f5485a61eefa5e686d6d5bdc7aa9ad6b104b1e92", pip=[PYTEST8, "click"], pythonpath=["src"],
        title="python-dotenv: an empty unquoted value followed by an inline comment is parsed as the comment",
        run="python -m pytest -q tests/test_parser.py tests/test_main.py",
        prompt="""python-dotenv mis-parses an empty unquoted value that is followed by an inline comment:

    >>> import io, dotenv
    >>> dotenv.dotenv_values(stream=io.StringIO("KEY= # comment"))
    {'KEY': '# comment'}            # wrong, the value is empty: {'KEY': ''}

Expected results (value of KEY):
- `KEY= # comment` -> ""   and   `KEY=  # comment` -> ""   and   `KEY=<tab># comment` -> ""
- `KEY= # comment` followed by a new line `OTHER=val` -> KEY is "" and OTHER is "val"
- `KEY=val # comment` -> "val"  (unchanged)
- No whitespace before the `#` means it is part of the value, as before: `KEY=a#b` -> "a#b", `KEY=#novalue` -> "#novalue", `KEY=# # comment` -> "#"

The low-level parser must agree: `dotenv.parser.parse_stream` should give a binding with value "" (and error False) for the text `a= #b` and for `a=  # b`.

The code is in src/dotenv/parser.py. Run the existing tests with: python -m pytest -q tests/test_parser.py tests/test_main.py""",
    ),
    dict(
        slug="xmltodict-unparse-none-values", dir="xmltodict", url="https://github.com/martinblech/xmltodict", license="MIT",
        fix="aa165113bef2b3a1a822209863343b9dc9ffe43a", pip=[PYTEST8], pythonpath=["."],
        title="xmltodict: unparse() writes the text 'None' for None values",
        run="python -m pytest -q tests/test_dicttoxml.py",
        prompt="""`xmltodict.unparse` writes the literal text "None" when a None value is inside an element dict:

    >>> from xmltodict import unparse
    >>> unparse({"a": {"@param": "test", "#text": None}}, full_document=False)
    '<a param="test">None</a>'      # wrong, should be '<a param="test"></a>'

None must be written as an empty value, for text and for attributes:
- `unparse({"a": {"@param": "test", "#text": None}}, full_document=False) == '<a param="test"></a>'`
- `unparse({"x": {"@pro": None}}, full_document=False) == '<x pro=""></x>'`
- `unparse({"x": {"#text": None, "@pro": None}, "y": None}, short_empty_elements=True, full_document=False) == '<x pro=""/><y/>'`
- `unparse({"a": None}, full_document=False)` equals `unparse({"a": {"#text": None}}, full_document=False)`.
Other non-string text (True, numbers) keeps its current output.

One existing test in tests/test_dicttoxml.py (test_non_string_text_with_attributes) asserts the old, wrong output for a None #text. It will be replaced by a corrected version, so update that line as needed.
The code is in xmltodict.py. Run the existing tests with: python -m pytest -q tests/test_dicttoxml.py""",
    ),
    dict(
        slug="toolz-interpose-empty", dir="toolz", url="https://github.com/pytoolz/toolz", license="BSD-3-Clause",
        fix="d2eba0374a452c2b18fe188b04527e22bda3548c", pip=[PYTEST8], pythonpath=["."],
        title="toolz: interpose() on an empty sequence raises StopIteration",
        run="python -m pytest -q toolz/tests/test_itertoolz.py",
        prompt="""`toolz.interpose` raises `StopIteration` when it is given an empty sequence:

    >>> from toolz import interpose
    >>> list(interpose("a", []))
    StopIteration                    # wrong: the result should be an empty iterator, so list(...) == []

`list(interpose("a", [1, 2, 3])) == [1, 'a', 2, 'a', 3]` must keep working.

The code is in toolz/itertoolz.py. Run the existing tests with: python -m pytest -q toolz/tests/test_itertoolz.py""",
    ),
]


def git(repo: Path, *args: str, text: bool = True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=text)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip() if text else r.stderr[-300:]}")
    return r.stdout


def is_test(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return "tests" in parts[:-1] or "test" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def snapshot(repo: Path, commit: str, dest: Path) -> None:
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            p = Path(m.name)
            if not m.isfile() or SKIP_DIRS & set(p.parts[:-1]) or p.suffix.lower() in SKIP_SUFFIX or m.size > MAX_BYTES:
                continue
            target = dest / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())


def main(clones: Path) -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    notes = ["# Set B: real bug fixes\n",
             "Each task is a real upstream bug fix. `repo/` is the project one commit before the fix (equivalently: the fix commit with its",
             "source change reverted). The fix commit's tests are the hidden check. Prompts are written by us from the upstream commit message and tests.\n",
             "| Task | Project | Licence | Fix commit | Parent commit |", "|---|---|---|---|---|"]
    for spec in SPECS:
        repo = clones / spec["dir"]
        fix = git(repo, "rev-parse", spec["fix"]).strip()
        parent = git(repo, "rev-parse", fix + "^").strip()
        changed = git(repo, "diff", "--name-only", parent, fix).splitlines()
        tests = [f for f in changed if is_test(f) and f.endswith(".py")]
        src = [f for f in changed if not is_test(f) and f.endswith(".py")]
        if not tests or not src:
            raise SystemExit(f"{spec['slug']}: expected both test and source files in the fix, got {changed}")
        task = OUT / spec["slug"]
        snapshot(repo, parent, task / "repo")
        for rel, text in spec.get("extra_files", {}).items():  # files the project generates at build time
            (task / "repo" / rel).parent.mkdir(parents=True, exist_ok=True)
            (task / "repo" / rel).write_text(text)
        for f in tests:
            (task / "check" / f).parent.mkdir(parents=True, exist_ok=True)
            (task / "check" / f).write_text(git(repo, "show", f"{fix}:{f}"))
        for f in src:
            (task / "gold" / f).parent.mkdir(parents=True, exist_ok=True)
            (task / "gold" / f).write_text(git(repo, "show", f"{fix}:{f}"))
        date = git(repo, "show", "-s", "--format=%cs", fix).strip()
        (task / "task.json").write_text(json.dumps({
            "id": f"B/{spec['slug']}", "set": "B", "title": spec["title"], "prompt": spec["prompt"],
            "authored_by": "upstream commit; prompt written by pinch-team",
            "env": {"pip": spec["pip"], "pythonpath": spec["pythonpath"]},
            "check": {"command": spec["run"].replace("python -m pytest -q", "python -m pytest -q -p no:cacheprovider", 1), "timeout": 600},
            "solution_files": src,
            "test_command": spec["run"],  # the command the prompt tells the agent; pinch's stop-on-green uses it too
            "source": {"repo": spec["url"], "base_commit": parent, "fix_commit": fix, "fix_date": date, "license": spec["license"],
                       "fix_url": f"{spec['url']}/commit/{fix}", "changed_source_files": src, "hidden_test_files": tests,
                       "added_generated_files": sorted(spec.get("extra_files", {}))},
        }, indent=2) + "\n")
        notes.append(f"| `{spec['slug']}` | [{spec['dir']}]({spec['url']}) | {spec['license']} | [`{fix[:10]}`]({spec['url']}/commit/{fix}) ({date}) | `{parent[:10]}` |")
        print(f"{spec['slug']}: fix {fix[:10]} parent {parent[:10]} tests {tests} src {src}")
    (OUT / "SOURCE.md").write_text("\n".join(notes) + "\n\nThe upstream LICENSE file of each project is kept at the top of its `repo/` folder. Built by `scripts/build_b_tasks.py`.\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
