# Benchmark report: setB-3.1-flash-lite.jsonl

Model: `gemini/gemini-3.1-flash-lite` · counted runs: 16 · excluded (infrastructure): 0 · sets: B

## Results per set and mode

Tokens are provider-reported `usage` (input + output) through Bifrost. *Tokens per solved task* = all tokens the group spent, failed runs included, divided by the runs that passed. *vs baseline* compares that figure with mini-swe-agent's.

### Set B: 8 tasks

| mode | runs | solved | avg tokens | median tokens | tokens / solved | vs baseline | cost / solved | avg calls | median time (no waits) |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 8 | 5 (62%) | 135,799 | 123,548 | 217,279 | - | $0.0404 | 24.1 | 120s |
| all | 8 | 5 (62%) | 89,898 | 62,510 | 143,837 | -34% | $0.0237 | 11.9 | 100s |

## Excluded runs

None.

## Methodology

- **Models.** Both agents use the same model through the same Bifrost gateway: `gemini/gemini-3.1-flash-lite`. Gateway: `http://127.0.0.1:8180/v1`; virtual-key header `x-bf-vk`. Cascade tiers: off / off.
- **Who answered.** Models the gateway reported as serving the calls: `gemini-3.1-flash-lite`. Calls answered by a fallback provider (recorded per call for pinch; for mini-swe-agent only when its response carries it): 0. The benchmark key has no fallback chain, so a different model should never answer silently.
- **Pacing and rate limits.** Both agents are paced the same way: at least 4.5 s between model requests inside each agent process (`--min-call-interval`; pinch through `PINCH_MIN_CALL_INTERVAL`, mini-swe-agent through a wrapper around its model call that changes nothing else), and both wait out a per-minute HTTP 429 (the provider's "retry in N s", at most 120 s, up to 8 times) instead of failing. Only a per-day limit stops a run. This report: 0 rate-limit waits, 518 s spent waiting in total. **Times in the tables exclude those waits.** Pacing is per process, so with `--jobs` above 1 the runs share one limit and the pacing no longer keeps each agent under it.
- **Agents.** pinch (this repository, `--auto-approve`) versus mini-swe-agent `2.4.6` (MIT, SWE-agent team) with litellm `1.108.3`, its default tool-calling config (`mini.yaml`), no changes except the connection. Each run: at most 30 model calls, 4096 output tokens per call, 900 s wall time, for both agents. pinch tool mode: `lazy`.
- **Tasks.** Set A: Python exercises from Aider's polyglot benchmark (not part of this run; Exercism, MIT; pinned commit 7e0611e, `bench/tasks/A/SOURCE.md`). Set B: real upstream bug fixes (8 used in this run; repository, fix commit and licence per task in `bench/tasks/B/SOURCE.md`; the starting tree is the commit before the fix, the fix's tests are the hidden check). Set C: docs tasks **written by us** (not part of this run; `bench/tasks/C/SOURCE.md`).
- **How pass is checked.** Not by the agent. After an agent stops, the harness restores the exercise's tests (A), or adds the upstream fix's tests the agent never saw (B), or adds our check script (C), deletes cached bytecode, and runs the check in a fresh container. Pass = exit code 0.
- **Sandbox.** Every agent run and every check happens in a throwaway container of the pinned image `python:3.12.15-slim-bookworm` with the task folder mounted and host networking (needed to reach the gateway). Model-generated commands never run on the host. Limitation: the container has internet access, so an agent could in principle fetch information from the web.
- **Auto-approval.** pinch ran with `--auto-approve`, so writes, shell commands and git operations were approved without a person. Every one is logged in the run's session file (`auto_approved` events): 76 in the 8 pinch runs in this report. mini-swe-agent has no approval gate in this configuration. This is a benchmark-only setting; pinch's normal mode asks first.
- **Tokens and cost.** Token counts are the `usage` the provider reported, relayed by Bifrost; nothing is estimated from text length. Cost = those tokens times `src/pinch/prices.toml`, the same table for both agents. A model without a price shows `n/a`.
- **Cache.** Real runs use a fresh Bifrost cache namespace each, so there are no cross-run cache hits. The 'same task twice' table is a separate demonstration.
- **Docs tool.** The Tavily `docs_lookup` tool is offered only on set C; on sets A and B `all` means every other technique (and `cascade` only when --small-model and --big-model are given). The techniques actually on for each run are recorded in its row (`techniques_on`).
- **Excluded.** Runs that failed on infrastructure (rate limit that did not clear, outage, exhausted quota or budget, bad credentials, harness error) are kept in the file with `status = infra_error` and left out of every number: 0 in this report. Rate limits are waited out and retried before a run is given up.
- **Limits.** One sample per cell unless `--repeat` was raised; models are not deterministic, so small differences are noise. `none` keeps pinch's default style (brief); the style table measures style on its own.
