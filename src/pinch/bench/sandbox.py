"""Run things inside a throwaway container. Nothing a model writes is ever run on the host.

One pinned image (python:3.12.15-slim-bookworm) is reused for everything. What the agents need lives in plain folders
under bench/.cache/tools/ that are mounted read-only into every container:

    tools/pinch/       a venv with pinch's two dependencies (httpx, turbomcp). pinch's source is mounted from this checkout.
    tools/mini/        a venv with mini-swe-agent (pinned) and its dependencies (litellm, ...)
    tools/envs/<id>/   one venv per distinct task environment (pytest plus whatever the task pins)

Containers use host networking so they reach the Bifrost gateway on 127.0.0.1, run as the host user (so files in the
work dir are not owned by root), have a memory and process cap, and are removed when they exit (`--rm`).
They carry the label pinch-bench=1 so `python -m pinch.bench clean` can remove any left behind after a crash.

Secrets are passed as `-e NAME` (the value is read from this process's environment by docker), never on the command line.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import CACHE_DIR

IMAGE = "python:3.12.15-slim-bookworm"  # pinned on purpose, never a floating tag
LABEL = "pinch-bench=1"
TOOLS_DIR = CACHE_DIR / "tools"
PINNED = {  # exact versions of the two things we install ourselves (see uv.lock for httpx and turbomcp)
    "mini-swe-agent": "2.4.6",
    "litellm": "1.108.3",  # what pip resolved when this was pinned; pinned explicitly so a rebuild gets the same one
    "openai": "3.46.0",
    "mcp": "2.3.0",  # litellm imports it as soon as a request carries tools
    "httpx": "0.28.1",
    "turbomcp": "0.3.0",
}
PINCH_SRC = Path(__file__).resolve().parents[1]  # .../src/pinch
CONTAINER_DIR = Path(__file__).resolve().parent / "container"
MEMORY = "2g"
CPUS = "2"
PIDS = "1024"
SECRET_ENV = ("PINCH_GATEWAY_KEY", "TAVILY_API_KEY")  # names only


class SandboxError(Exception):
    pass


@dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    seconds: float


def docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def image_present() -> bool:
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    return r.returncode == 0


def ensure_image() -> None:
    if image_present():
        return
    r = subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True, text=True)
    if r.returncode != 0:
        raise SandboxError(f"could not pull {IMAGE}: {r.stderr.strip()[-300:]}")


def container_run(command: list[str], *, mounts: dict[str, tuple[Path, str]] | None = None, env: dict[str, str] | None = None,
                  secrets: tuple[str, ...] = (), workdir: str = "/work", timeout: int = 600, name: str | None = None,
                  network: bool = True) -> Run:
    """Run `command` in a fresh container of the pinned image.

    mounts: container path -> (host path, "ro"|"rw").  env: plain (non-secret) variables.
    secrets: names of variables to pass through from the current process environment without showing their values.
    On timeout the container is killed and removed."""
    name = name or f"pinch-bench-{os.getpid()}-{uuid.uuid4().hex[:10]}"
    cmd = ["docker", "run", "--rm", "--name", name, "--label", LABEL, "-u", f"{os.getuid()}:{os.getgid()}",
           "--memory", MEMORY, "--cpus", CPUS, "--pids-limit", PIDS, "-w", workdir]
    if network:
        cmd += ["--network", "host"]
    else:
        cmd += ["--network", "none"]
    for target, (host, mode) in (mounts or {}).items():
        cmd += ["-v", f"{host}:{target}:{mode}"]
    for key, value in {"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", **(env or {})}.items():
        cmd += ["-e", f"{key}={value}"]
    for key in secrets:
        if key in os.environ:
            cmd += ["-e", key]  # value is inherited from os.environ by docker; it is not on this command line
    cmd += [IMAGE, *command]
    start = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        if r.returncode == 125 or (r.returncode in (126, 127) and "docker:" in r.stderr):
            # docker itself failed (name clash, daemon error, missing image): that is never a test result
            raise SandboxError("docker could not run the container: " + r.stderr.strip()[-300:])
        return Run(r.returncode, r.stdout, r.stderr, False, time.time() - start)
    except subprocess.TimeoutExpired as e:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        out = (e.stdout or b"")
        err = (e.stderr or b"")
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
        return Run(124, out, err, True, time.time() - start)


def clean_containers() -> int:
    """Remove every container this harness started (left over after a crash). Returns how many."""
    r = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={LABEL}"], capture_output=True, text=True)
    ids = r.stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
    return len(ids)


# -- tool venvs -------------------------------------------------------------------------------------

def _exists(path: Path) -> bool:
    """A venv's python is a symlink to a path that only exists inside the container, so Path.exists() would say no."""
    return os.path.lexists(path)


def _tools_mount() -> dict[str, tuple[Path, str]]:
    return {"/opt/tools": (TOOLS_DIR, "rw")}


def _install(venv: str, packages: list[str], timeout: int = 1800) -> None:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    script = (f"rm -rf /opt/tools/{venv} && python -m venv /opt/tools/{venv} && "
              f"/opt/tools/{venv}/bin/pip install -q --no-cache-dir {' '.join(packages)} && "
              f"/opt/tools/{venv}/bin/pip freeze > /opt/tools/{venv}.freeze")
    r = container_run(["sh", "-c", script], mounts=_tools_mount(), env={"PIP_NO_CACHE_DIR": "1"}, timeout=timeout, workdir="/tmp")
    if r.returncode != 0:
        raise SandboxError(f"installing {venv} failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-600:]}")


def setup_tools(force: bool = False) -> list[str]:
    """Build the pinch and mini-swe-agent venvs once. Returns what was built."""
    ensure_image()
    built = []
    for venv, packages in (("pinch", [f"httpx=={PINNED['httpx']}", f"turbomcp=={PINNED['turbomcp']}"]),
                           ("mini", [f"mini-swe-agent=={PINNED['mini-swe-agent']}", f"litellm=={PINNED['litellm']}", f"openai=={PINNED['openai']}", f"mcp=={PINNED['mcp']}"])):
        if force or not _exists(TOOLS_DIR / venv / "bin" / "python") or not _exists(TOOLS_DIR / f"{venv}.freeze"):
            _install(venv, packages)
            built.append(venv)
    return built


def tool_versions() -> dict[str, str]:
    """name==version lines of the key packages in each tool venv (from the pip freeze written at setup)."""
    out = {}
    for venv, wanted in (("mini", ("mini-swe-agent", "litellm", "openai", "mcp")), ("pinch", ("httpx", "turbomcp"))):
        f = TOOLS_DIR / f"{venv}.freeze"
        if f.is_file():
            for line in f.read_text().splitlines():
                name, _, version = line.partition("==")
                if name.lower().replace("_", "-") in wanted:
                    out[name.lower().replace("_", "-")] = version
    return out


def env_name(pip: tuple[str, ...]) -> str:
    return "py-" + hashlib.sha1("\n".join(sorted(pip)).encode()).hexdigest()[:10]


def ensure_env(pip: tuple[str, ...]) -> str:
    """The venv a task needs (built the first time, then reused). Returns its folder name under tools/envs/."""
    name = env_name(pip)
    marker = TOOLS_DIR / "envs" / name / "bin" / "python"
    if not _exists(marker):
        ensure_image()
        (TOOLS_DIR / "envs").mkdir(parents=True, exist_ok=True)
        pkgs = " ".join(f"'{p}'" for p in pip) or ""
        script = (f"rm -rf /opt/tools/envs/{name} && python -m venv /opt/tools/envs/{name} && "
                  f"/opt/tools/envs/{name}/bin/pip install -q --no-cache-dir {pkgs}")
        r = container_run(["sh", "-c", script], mounts=_tools_mount(), env={"PIP_NO_CACHE_DIR": "1"}, timeout=900, workdir="/tmp")
        if r.returncode != 0:
            raise SandboxError(f"building the environment {name} ({', '.join(pip)}) failed: {(r.stderr or r.stdout).strip()[-600:]}")
    return name


def task_env(env_folder: str, pythonpath: tuple[str, ...]) -> dict[str, str]:
    """Environment variables every command of a task sees: its venv first on PATH, its source folders on PYTHONPATH."""
    path = f"/opt/tools/envs/{env_folder}/bin:/usr/local/bin:/usr/bin:/bin"
    pp = ":".join("/work" if p in (".", "") else f"/work/{p}" for p in pythonpath)
    out = {"PATH": path, "VIRTUAL_ENV": f"/opt/tools/envs/{env_folder}", "PYTHONDONTWRITEBYTECODE": "1"}
    if pp:
        out["PYTHONPATH"] = pp
    return out


def task_mounts(work: Path, out: Path | None = None, extra_ro: dict[str, Path] | None = None) -> dict[str, tuple[Path, str]]:
    mounts = {"/work": (work, "rw"), "/opt/tools": (TOOLS_DIR, "ro"), "/opt/pinch/src/pinch": (PINCH_SRC, "ro"),
              "/opt/bench": (CONTAINER_DIR, "ro")}
    if out is not None:
        mounts["/out"] = (out, "rw")
    for target, host in (extra_ro or {}).items():
        mounts[target] = (host, "ro")
    return mounts
