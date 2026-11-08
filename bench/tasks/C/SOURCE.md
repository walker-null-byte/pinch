# Set C: docs tasks, WRITTEN BY US

These five tasks were written by the pinch team (not taken from any public benchmark) for the Tavily docs on/off comparison.
Each needs a feature that appeared in a 2026 release of a library, with the exact version pinned in the task's environment,
so a model cannot be expected to know the API from training data. Each has a hidden behavioural check (`check/_bench_check.py`)
and a reference solution (`gold/`) that was run against it.

| Task | Library and feature |
|---|---|
| click-argument-help | click 8.5.0 (2026-08-24): `Argument(help=...)` and the "Positional arguments" help section |
| click-custom-version-option | click 8.5.0: `click.custom_version_option` |
| pytest-max-warnings | pytest 9.1.0 (2026-06-13): the `max_warnings` option |
| pytest-long-string-ids | pytest 9.2.0 (2026-10-27): `parametrize_long_str_id_strategy` |
| anyio-portal-call-async | anyio 4.16.0 (2026-11-01): `BlockingPortal.call_async` |

Caveat: the container has network access (it needs it to reach the gateway), and the installed packages carry their own docstrings,
so an agent can also find the API by reading the library's source. That is a legitimate route and is part of what the comparison measures.
