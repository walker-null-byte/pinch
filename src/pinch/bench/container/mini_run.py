"""Runs inside the sandbox container: mini-swe-agent (pinned) against the Bifrost gateway.

Started as: /opt/tools/mini/bin/python -I /opt/bench/mini_run.py /out/mini_spec.json

The agent, its prompts and its bash tool are mini-swe-agent's own (config/mini.yaml, the default tool-calling setup).
Only the connection is ours: litellm is pointed at Bifrost's OpenAI-compatible endpoint and the virtual-key header
`x-bf-vk` is added to every request. The key is read from the environment, removed from it before any command can
run, and added to the request at call time, so it never lands in the trajectory file or in a command's environment.
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, "/opt/pinch/src")  # pinch's rate-limit policy (standard library only), so both agents wait the same way
from pinch import ratelimit  # noqa: E402

spec = json.loads(Path(sys.argv[1]).read_text())
KEY = os.environ.pop("PINCH_GATEWAY_KEY", "")
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # no network call to fetch litellm's price map
os.environ.setdefault("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", str(spec.get("retry_attempts", 6)))

import litellm  # noqa: E402

litellm.telemetry = False
_completion = litellm.completion


# Pacing and rate-limit waits: the same policy pinch's client applies (pinch/ratelimit.py, pinch/client.py). Nothing about mini's prompts,
# tools or config changes; only WHEN a request is sent, and waiting out a per-minute 429 instead of failing the run.
MIN_INTERVAL = float(spec.get("min_call_interval") or 0)
STATS = {"wait_seconds": 0.0, "rate_limit_waits": 0}
_last_request = [0.0]


def _pace():
    if MIN_INTERVAL > 0:
        gap = _last_request[0] + MIN_INTERVAL - time.monotonic()
        if gap > 0:
            time.sleep(gap)
            STATS["wait_seconds"] += gap
        _last_request[0] = time.monotonic()


def _retry_after(exc):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    return headers.get("retry-after") if headers else None


def completion(*args, **kwargs):
    headers = dict(kwargs.get("extra_headers") or {})
    headers["x-bf-vk"] = KEY
    kwargs["extra_headers"] = headers
    limited = 0
    while True:
        _pace()
        try:
            return _completion(*args, **kwargs)
        except Exception as exc:
            text = str(exc)
            if not (getattr(exc, "status_code", None) == 429 or type(exc).__name__ == "RateLimitError"):
                raise
            if ratelimit.is_daily_limit(text):
                raise  # a per-day limit does not clear by waiting
            limited += 1
            if limited > ratelimit.RATE_LIMIT_RETRIES:
                raise
            seconds = ratelimit.wait_seconds(text, _retry_after(exc), limited, 1.0)
            print(f"rate limited: waiting {seconds:.0f} s, then retrying ({limited} of {ratelimit.RATE_LIMIT_RETRIES})", file=sys.stderr, flush=True)
            time.sleep(seconds)
            STATS["wait_seconds"] += seconds
            STATS["rate_limit_waits"] += 1


litellm.completion = completion

from minisweagent.agents import get_agent  # noqa: E402
from minisweagent.config import builtin_config_dir, get_config_from_spec  # noqa: E402
from minisweagent.environments import get_environment  # noqa: E402
from minisweagent.models import get_model  # noqa: E402
from minisweagent.utils.serialize import recursive_merge  # noqa: E402

result_path = Path(spec["result"])
result = {"exit_status": "", "error_type": "", "error": ""}
try:
    base = get_config_from_spec(str(builtin_config_dir / "mini.yaml"))
    config = recursive_merge(base, {
        "agent": {"agent_class": "default", "step_limit": spec["max_steps"], "cost_limit": 0.0,
                  "wall_time_limit_seconds": spec.get("wall_time", 0), "output_path": spec["trajectory"]},
        "model": {"model_name": "openai/" + spec["model"], "cost_tracking": "ignore_errors",
                  "model_kwargs": {"api_base": spec["api_base"], "api_key": "bifrost-virtual-key-in-header",
                                   "max_tokens": spec["max_tokens"], "drop_params": True}},
        "environment": {"environment_class": "local", "cwd": "/work", "timeout": spec.get("command_timeout", 180)},
    })
    model = get_model(config=config.get("model", {}))
    env = get_environment(config.get("environment", {}), default_type="local")
    agent = get_agent(model, env, config.get("agent", {}), default_type="default")
    info = agent.run(spec["task"])
    result["exit_status"] = (info or {}).get("exit_status", "")
except BaseException as e:  # the harness classifies the exception; a crash here must still leave a result file
    result["exit_status"] = type(e).__name__
    result["error_type"] = type(e).__name__
    scrub = (lambda text: text.replace(KEY, "[key]") if KEY else text)
    result["error"] = scrub(str(e) or "")[:2000]
    result["traceback"] = scrub(traceback.format_exc())[-3000:]
finally:
    result.update(wait_seconds=round(STATS["wait_seconds"], 1), rate_limit_waits=STATS["rate_limit_waits"])
    result_path.write_text(json.dumps(result))
sys.exit(0 if result["exit_status"] == "Submitted" else 1)
