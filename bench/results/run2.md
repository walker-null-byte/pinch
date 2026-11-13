# Benchmark report: run2.jsonl

Model: `gemini/gemini-3.1-flash-lite` · counted runs: 68 · excluded (infrastructure): 3 · sets: A, B

## Results per set and mode

Tokens are provider-reported `usage` (input + output) through Bifrost. *Tokens per solved task* = all tokens the group spent, failed runs included, divided by the runs that passed. *vs baseline* compares that figure with mini-swe-agent's.

### Set A: 5 tasks

| mode | runs | solved | avg tokens | median tokens | tokens / solved | vs baseline | cost / solved | avg calls | median time (no waits) |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 5 | 4 (80%) | 151,916 | 69,011 | 189,896 | - | $0.0264 | 11.8 | 138s |
| none | 5 | 4 (80%) | 167,409 | 54,226 | 209,262 | +10% | $0.0278 | 16.0 | 79s |
| all | 5 | 3 (60%) | 93,883 | 5,677 | 156,472 | -18% | $0.0251 | 12.8 | 35s |

Paired: only tasks that finished in every mode above. 2 other runs of set A are left out of this table until the other modes finish them.

### Set B: 8 tasks

| mode | runs | solved | avg tokens | median tokens | tokens / solved | vs baseline | cost / solved | avg calls | median time (no waits) |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 8 | 5 (62%) | 135,799 | 123,548 | 217,279 | - | $0.0404 | 24.1 | 120s |
| all | 8 | 5 (62%) | 89,898 | 62,510 | 143,837 | -34% | $0.0237 | 11.9 | 100s |

## Ablation: what each technique contributes

`all` minus one technique, compared with `all` on the same tasks. *Extra tokens without it* = tokens per solved task of `no-<technique>` minus that of `all`. *Share of savings* = that extra divided by (`none` minus `all`): how much of the total saving the technique accounts for. Negative = the technique cost tokens here. Shares of techniques that overlap do not add up to 100%. A technique that changes nothing in a run (for example `cache` when no request repeats) shows ~0.

### Set A (5 paired runs per mode)

| mode | runs | pass rate | tokens / solved | extra tokens without it | share of savings | cached input share |
|---|---|---|---|---|---|---|
| none (no techniques) | 5 | 80% | 209,262 |  |  | 63% |
| all | 5 | 60% | 156,472 |  |  | 47% |
| no-context | 5 | 60% | 103,734 | -52,738 | -100% | 32% |
| no-filememory | 5 | 80% | 79,698 | -76,774 | -145% | 28% |
| no-smalledits | 5 | 80% | 50,136 | -106,336 | -201% | 25% |
| no-testcompress | 5 | 100% | 41,017 | -115,455 | -219% | 20% |
| no-trim | 5 | 60% | 150,412 | -6,060 | -11% | 52% |
| no-pretest | 5 | 80% | 61,894 | -94,578 | -179% | 28% |
| no-stopongreen | 5 | 80% | 109,008 | -47,464 | -90% | 40% |

**Set A, only the 3 tasks every mode above solved.** One failed run can spend 30 calls and decide the table above on its own; here pass/fail is the same for every mode, so the difference is what the technique saves (or costs) when the task gets done. *vs all*: how many more tokens the same tasks took with that technique off (negative = fewer).

| mode | total tokens | avg calls | vs all |
|---|---|---|---|
| none | 91,122 | 8.3 | +600% |
| all | 13,017 | 1.3 | - |
| no-context | 27,520 | 3.7 | +111% |
| no-filememory | 32,638 | 2.7 | +151% |
| no-smalledits | 12,862 | 1.3 | -1% |
| no-testcompress | 16,463 | 1.7 | +26% |
| no-trim | 16,365 | 1.7 | +26% |
| no-pretest | 37,083 | 3.7 | +185% |
| no-stopongreen | 73,959 | 6.3 | +468% |

**Trim versus the provider's prompt cache (set A).** Provider-reported cached input tokens decide this; the default is left as is.

- trim on (`all`): 156,472 tokens per solved task, 47% of input tokens served from the provider's prompt cache
- trim off (`no-trim`): 150,412 (-4%), cached share 52%
- Cached tokens are cheaper than fresh ones, so compare cost per solved task as well as tokens: see the main table. If the provider reports no cached tokens at all (cached share 0%), the data cannot favour either side.


## Excluded runs

These runs failed on infrastructure, not on the task. They stay in the results file, marked `infra_error`, and are in no average. A later retry of the same run, if it finished, is counted instead.

| reason | runs |
|---|---|
| quota | 2 |
| bad_request | 1 |

## Methodology

- **Models.** Both agents use the same model through the same Bifrost gateway: `gemini/gemini-3.1-flash-lite`. Gateway: `http://127.0.0.1:8180/v1`; virtual-key header `x-bf-vk`. Cascade tiers: off / off.
- **Who answered.** Models the gateway reported as serving the calls: `gemini-3.1-flash-lite`. Calls answered by a fallback provider (recorded per call for pinch; for mini-swe-agent only when its response carries it): 0. The benchmark key has no fallback chain, so a different model should never answer silently.
- **Pacing and rate limits.** Both agents are paced the same way: at least 4.5 s between model requests inside each agent process (`--min-call-interval`; pinch through `PINCH_MIN_CALL_INTERVAL`, mini-swe-agent through a wrapper around its model call that changes nothing else), and both wait out a per-minute HTTP 429 (the provider's "retry in N s", at most 120 s, up to 8 times) instead of failing. Only a per-day limit stops a run. This report: 0 rate-limit waits, 1,121 s spent waiting in total. **Times in the tables exclude those waits.** Pacing is per process, so with `--jobs` above 1 the runs share one limit and the pacing no longer keeps each agent under it.
- **Agents.** pinch (this repository, `--auto-approve`) versus mini-swe-agent `2.4.6` (MIT, SWE-agent team) with litellm `1.108.3`, its default tool-calling config (`mini.yaml`), no changes except the connection. Each run: at most 30 model calls, 4096 output tokens per call, 900 s wall time, for both agents. pinch tool mode: `lazy`.
- **Tasks.** Set A: Python exercises from Aider's polyglot benchmark (7 used in this run; Exercism, MIT; pinned commit 7e0611e, `bench/tasks/A/SOURCE.md`). Set B: real upstream bug fixes (8 used in this run; repository, fix commit and licence per task in `bench/tasks/B/SOURCE.md`; the starting tree is the commit before the fix, the fix's tests are the hidden check). Set C: docs tasks **written by us** (0 used in this run; `bench/tasks/C/SOURCE.md`).
- **How pass is checked.** Not by the agent. After an agent stops, the harness restores the exercise's tests (A), or adds the upstream fix's tests the agent never saw (B), or adds our check script (C), deletes cached bytecode, and runs the check in a fresh container. Pass = exit code 0.
- **Sandbox.** Every agent run and every check happens in a throwaway container of the pinned image `python:3.12.15-slim-bookworm` with the task folder mounted and host networking (needed to reach the gateway). Model-generated commands never run on the host. Limitation: the container has internet access, so an agent could in principle fetch information from the web.
- **Auto-approval.** pinch ran with `--auto-approve`, so writes, shell commands and git operations were approved without a person. Every one is logged in the run's session file (`auto_approved` events): 568 in the 55 pinch runs in this report. mini-swe-agent has no approval gate in this configuration. This is a benchmark-only setting; pinch's normal mode asks first.
- **Tokens and cost.** Token counts are the `usage` the provider reported, relayed by Bifrost; nothing is estimated from text length. Cost = those tokens times `src/pinch/prices.toml`, the same table for both agents. A model without a price shows `n/a`.
- **Cache.** Real runs use a fresh Bifrost cache namespace each, so there are no cross-run cache hits. The 'same task twice' table is a separate demonstration.
- **Docs tool.** The Tavily `docs_lookup` tool is offered only on set C; on sets A and B `all` means every other technique (and `cascade` only when --small-model and --big-model are given). The techniques actually on for each run are recorded in its row (`techniques_on`).
- **Excluded.** Runs that failed on infrastructure (rate limit that did not clear, outage, exhausted quota or budget, bad credentials, harness error) are kept in the file with `status = infra_error` and left out of every number: 3 in this report. Rate limits are waited out and retried before a run is given up.
- **Limits.** One sample per cell unless `--repeat` was raised; models are not deterministic, so small differences are noise. `none` keeps pinch's default style (brief); the style table measures style on its own.
