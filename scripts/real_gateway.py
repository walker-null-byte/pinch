"""Start, stop and check the real-provider gateway without Docker Compose.

    uv run python scripts/real_gateway.py up       # start pinch-bifrost-real (same settings as gateway/docker-compose.real.yml)
    uv run python scripts/real_gateway.py status   # is it healthy? which image?
    uv run python scripts/real_gateway.py down     # stop and remove the container (the named volume, and so the spend, stays)
    uv run python scripts/real_gateway.py down --volume   # also delete the data volume: budgets start again from zero
    uv run python scripts/real_gateway.py reset vk-demo-low   # set one key's spend back to zero (to repeat the budget demo)

Some machines have Docker without the Compose plugin. This script reads the SAME file (gateway/docker-compose.real.yml): its image,
container name, environment block and mounts become one `docker run`. Secret values are read from pinch/.env inside this script and
handed to Docker by name (`-e NAME`), so they never appear on a command line or in this script's output.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "gateway" / "docker-compose.real.yml"
ENV_FILE = ROOT / ".env"
URL = "http://127.0.0.1:8180"


def read_compose() -> dict:
    """The few fields of the compose file this script needs (it has no YAML library)."""
    text = COMPOSE.read_text(encoding="utf-8")
    image = re.search(r"^\s+image:\s*(\S+)", text, re.M).group(1)
    name = re.search(r"^\s+container_name:\s*(\S+)", text, re.M).group(1)
    volume = re.search(r"^volumes:\s*\n\s+\S+:\s*\n\s+name:\s*(\S+)", text, re.M).group(1)
    env_block = re.search(r"^    environment:\n((?:      .*\n)+)", text, re.M).group(1)
    env: dict[str, str | None] = {}  # name -> literal value, or None when it comes from .env
    for line in env_block.splitlines():
        key, _, value = line.strip().partition(":")
        value = value.strip()
        env[key] = None if value.startswith("${") else value.strip('"')
    return {"image": image, "name": name, "volume": volume, "env": env}


def load_dotenv(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip("'\"")
    return out


def healthy() -> bool:
    try:
        return httpx.get(URL + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def up(c: dict) -> int:
    dotenv = load_dotenv(ENV_FILE)
    missing = [k for k, v in c["env"].items() if v is None and not dotenv.get(k)]
    if missing:
        print("missing or empty in .env: " + ", ".join(missing) + "\nRun: uv run python scripts/init_env.py")
        return 2
    subprocess.run(["docker", "rm", "-f", c["name"]], capture_output=True)
    cmd = ["docker", "run", "-d", "--name", c["name"], "--network", "host", "--restart", "no"]
    for key, literal in c["env"].items():
        cmd += ["-e", key if literal is None else f"{key}={literal}"]
    cmd += ["-v", f"{c['volume']}:/app/data", "-v", f"{ROOT / 'gateway' / 'config.real.json'}:/app/data/config.json:ro", c["image"]]
    run = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **{k: dotenv[k] for k, v in c["env"].items() if v is None}})
    if run.returncode != 0:
        print("docker could not start the gateway: " + run.stderr.strip()[-300:])
        return 1
    deadline = time.time() + 90
    while time.time() < deadline:
        if healthy():
            print(f"{c['name']} is up on {URL} (image {c['image']})")
            return 0
        time.sleep(1)
    print(f"{c['name']} did not become healthy in 90 s; see: docker logs {c['name']}")
    return 1


def down(c: dict, volume: bool) -> int:
    subprocess.run(["docker", "rm", "-f", c["name"]], capture_output=True)
    print(f"{c['name']} removed")
    if volume:
        subprocess.run(["docker", "volume", "rm", c["volume"]], capture_output=True)
        print(f"volume {c['volume']} removed: budgets start from zero next time")
    return 0


def status(c: dict) -> int:
    r = subprocess.run(["docker", "ps", "--filter", f"name=^{c['name']}$", "--format", "{{.Names}}  {{.Image}}  {{.Status}}"],
                       capture_output=True, text=True)
    print(r.stdout.strip() or f"{c['name']} is not running")
    print("health: " + ("ok" if healthy() else "not answering on " + URL))
    return 0 if healthy() else 1


def reset(vk_id: str) -> int:
    """Zero one virtual key's budget usage through the admin API (the same call `pinch prove` uses for its demo users)."""
    env = load_dotenv(ENV_FILE)
    with httpx.Client(base_url=URL, auth=(env.get("PINCH_GATEWAY_ADMIN_USER", ""), env.get("PINCH_GATEWAY_ADMIN_PASSWORD", "")), timeout=10) as http:
        got = http.get(f"/api/governance/virtual-keys/{vk_id}")
        if got.status_code != 200:
            print(f"cannot read {vk_id} (HTTP {got.status_code}): is the gateway up and the admin login right?")
            return 1
        budgets = [{"id": b["id"], "max_limit": b["max_limit"], "reset_duration": b["reset_duration"]} for b in got.json()["virtual_key"].get("budgets") or []]
        put = http.put(f"/api/governance/virtual-keys/{vk_id}", json={"budgets": budgets, "reset_budget_usage": True})
    print(f"{vk_id}: spend reset to zero" if put.status_code == 200 else f"reset failed (HTTP {put.status_code})")
    return 0 if put.status_code == 200 else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("action", choices=("up", "down", "status", "reset"))
    p.add_argument("vk_id", nargs="?", help="with `reset`: the virtual key id, e.g. vk-demo-low")
    p.add_argument("--volume", action="store_true", help="with `down`: also delete the data volume")
    args = p.parse_args()
    c = read_compose()
    if args.action == "reset":
        return reset(args.vk_id) if args.vk_id else p.error("reset needs a virtual key id, e.g. vk-demo-low")
    return {"up": lambda: up(c), "down": lambda: down(c, args.volume), "status": lambda: status(c)}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
