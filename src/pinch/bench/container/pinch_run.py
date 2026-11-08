"""Runs inside the sandbox container: pinch's CLI, with the secrets kept out of the environment the tools see.

Started as: /opt/tools/pinch/bin/python -I /opt/bench/pinch_run.py <pinch arguments>
(-I so the task's PYTHONPATH cannot shadow pinch's own imports; pinch's source is added to sys.path here.)
"""

import os
import sys

sys.path.insert(0, "/opt/pinch/src")

SECRETS = ("PINCH_GATEWAY_KEY", "TAVILY_API_KEY")
held = {k: os.environ.pop(k) for k in SECRETS if k in os.environ}

import pinch.cli as cli  # noqa: E402

_load = cli.load_config


def load_config_with_secrets(*args, **kwargs):
    """Put the secrets back only while the configuration is read, so commands the agent runs never inherit them."""
    os.environ.update(held)
    try:
        return _load(*args, **kwargs)
    finally:
        for k in held:
            os.environ.pop(k, None)


cli.load_config = load_config_with_secrets
sys.exit(cli.main(sys.argv[1:]))
