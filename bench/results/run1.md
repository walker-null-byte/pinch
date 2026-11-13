# Benchmark report: run1.jsonl

Model: `gemini/gemini-3.5-flash-lite` · counted runs: 72 · excluded (infrastructure): 2 · sets: A, B, C

## Results per set and mode

Tokens are provider-reported `usage` (input + output) through Bifrost. *Tokens per solved task* = all tokens the group spent, failed runs included, divided by the runs that passed. *vs baseline* compares that figure with mini-swe-agent's.

### Set A: 23 tasks

| mode | runs | solved | avg tokens | median tokens | tokens / solved | vs baseline | cost / solved | avg calls | median time (no waits) |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 23 | 23 (100%) | 112,537 | 50,177 | 112,537 | - | $0.0220 | 10.3 | 58s |
| all | 23 | 23 (100%) | 23,217 | 12,031 | 23,217 | -79% | $0.0076 | 4.0 | 10s |

### Set B: 8 tasks

| mode | runs | solved | avg tokens | median tokens | tokens / solved | vs baseline | cost / solved | avg calls | median time (no waits) |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 8 | 7 (88%) | 202,868 | 169,235 | 231,849 | - | $0.0490 | 20.8 | 231s |
| all | 8 | 5 (62%) | 125,904 | 110,591 | 201,447 | -13% | $0.0328 | 15.1 | 37s |

### Set C: 5 tasks

| mode | runs | solved | avg tokens | median tokens | tokens / solved | vs baseline | cost / solved | avg calls | median time (no waits) |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 5 | 4 (80%) | 204,347 | 241,969 | 255,434 | - | $0.0470 | 24.2 | 168s |
| all | 5 | 3 (60%) | 52,806 | 23,835 | 88,011 | -66% | $0.0273 | 16.8 | 67s |

## Excluded runs

These runs failed on infrastructure, not on the task. They stay in the results file, marked `infra_error`, and are in no average. A later retry of the same run, if it finished, is counted instead.

| reason | runs |
|---|---|
| quota | 2 |

## Methodology

- **Models.** Both agents use the same model through the same Bifrost gateway: `gemini/gemini-3.5-flash-lite`. Gateway: `http://127.0.0.1:8180/v1`; virtual-key header `x-bf-vk`. Cascade tiers: off / off.
- **Who answered.** Models the gateway reported as serving the calls: `gemini-3.5-flash-lite`. Calls answered by a fallback provider (recorded per call for pinch; for mini-swe-agent only when its response carries it): 0. The benchmark key has no fallback chain, so a different model should never answer silently.
- **Pacing and rate limits.** Both agents are paced the same way: at least 4.5 s between model requests inside each agent process (1 earlier session(s) in this file ran before pacing was recorded) (`--min-call-interval`; pinch through `PINCH_MIN_CALL_INTERVAL`, mini-swe-agent through a wrapper around its model call that changes nothing else), and both wait out a per-minute HTTP 429 (the provider's "retry in N s", at most 120 s, up to 8 times) instead of failing. Only a per-day limit stops a run. This report: 2 rate-limit waits, 1,121 s spent waiting in total. **Times in the tables exclude those waits.** Pacing is per process, so with `--jobs` above 1 the runs share one limit and the pacing no longer keeps each agent under it.
- **Agents.** pinch (this repository, `--auto-approve`) versus mini-swe-agent `2.4.6` (MIT, SWE-agent team) with litellm `1.108.3`, its default tool-calling config (`mini.yaml`), no changes except the connection. Each run: at most 30 model calls, 4096 output tokens per call, 900 s wall time, for both agents. pinch tool mode: `lazy`.
- **Tasks.** Set A: Python exercises from Aider's polyglot benchmark (34 used in this run; Exercism, MIT; pinned commit 7e0611e, `bench/tasks/A/SOURCE.md`). Set B: real upstream bug fixes (8 used in this run; repository, fix commit and licence per task in `bench/tasks/B/SOURCE.md`; the starting tree is the commit before the fix, the fix's tests are the hidden check). Set C: docs tasks **written by us** (5 used in this run; `bench/tasks/C/SOURCE.md`).
- **How pass is checked.** Not by the agent. After an agent stops, the harness restores the exercise's tests (A), or adds the upstream fix's tests the agent never saw (B), or adds our check script (C), deletes cached bytecode, and runs the check in a fresh container. Pass = exit code 0.
- **Sandbox.** Every agent run and every check happens in a throwaway container of the pinned image `python:3.12.15-slim-bookworm` with the task folder mounted and host networking (needed to reach the gateway). Model-generated commands never run on the host. Limitation: the container has internet access, so an agent could in principle fetch information from the web.
- **Auto-approval.** pinch ran with `--auto-approve`, so writes, shell commands and git operations were approved without a person. Every one is logged in the run's session file (`auto_approved` events): 266 in the 36 pinch runs in this report. mini-swe-agent has no approval gate in this configuration. This is a benchmark-only setting; pinch's normal mode asks first.
- **Tokens and cost.** Token counts are the `usage` the provider reported, relayed by Bifrost; nothing is estimated from text length. Cost = those tokens times `src/pinch/prices.toml`, the same table for both agents. A model without a price shows `n/a`.
- **Cache.** Real runs use a fresh Bifrost cache namespace each, so there are no cross-run cache hits. The 'same task twice' table is a separate demonstration.
- **Docs tool.** The Tavily `docs_lookup` tool is offered only on set C; on sets A and B `all` means every other technique (and `cascade` only when --small-model and --big-model are given). The techniques actually on for each run are recorded in its row (`techniques_on`).
- **Excluded.** Runs that failed on infrastructure (rate limit that did not clear, outage, exhausted quota or budget, bad credentials, harness error) are kept in the file with `status = infra_error` and left out of every number: 2 in this report. Rate limits are waited out and retried before a run is given up.
- **Limits.** One sample per cell unless `--repeat` was raised; models are not deterministic, so small differences are noise. `none` keeps pinch's default style (brief); the style table measures style on its own.
