# Benchmark report: run2.jsonl

Model: `gemini/gemini-3.1-flash-lite` · counted runs: 106 · excluded (infrastructure): 4 · sets: A, B

## Results per set and mode

Tokens are provider-reported `usage` (input + output) through Bifrost. *Tokens per solved task* = all tokens the group spent, failed runs included, divided by the runs that passed. *vs baseline* compares that figure with mini-swe-agent's.

### Set A: 7 tasks

| mode | runs | solved | avg tokens | median tokens | tokens / solved | vs baseline | cost / solved | avg calls | median time (no waits) |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 7 | 6 (86%) | 138,545 | 77,815 | 161,636 | - | $0.0256 | 12.9 | 138s |
| none | 7 | 5 (71%) | 181,704 | 101,471 | 254,385 | +57% | $0.0342 | 18.0 | 65s |
| all | 7 | 5 (71%) | 81,687 | 48,393 | 114,362 | -29% | $0.0203 | 11.3 | 35s |

### Set B: 3 tasks

| mode | runs | solved | avg tokens | median tokens | tokens / solved | vs baseline | cost / solved | avg calls | median time (no waits) |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 | 2 (67%) | 156,678 | 101,532 | 235,018 | - | $0.0406 | 23.0 | 117s |
| none | 3 | 3 (100%) | 171,743 | 140,437 | 171,743 | -27% | $0.0224 | 18.7 | 116s |
| all | 3 | 2 (67%) | 68,904 | 41,899 | 103,356 | -56% | $0.0179 | 7.7 | 73s |

Paired: only tasks that finished in every mode above. 10 other runs of set B are left out of this table until the other modes finish them.

## Ablation: what each technique contributes

`all` minus one technique, compared with `all` on the same tasks. *Extra tokens without it* = tokens per solved task of `no-<technique>` minus that of `all`. *Share of savings* = that extra divided by (`none` minus `all`): how much of the total saving the technique accounts for. Negative = the technique cost tokens here. Shares of techniques that overlap do not add up to 100%. A technique that changes nothing in a run (for example `cache` when no request repeats) shows ~0.

### Set A (7 paired runs per mode)

| mode | runs | pass rate | tokens / solved | extra tokens without it | share of savings | cached input share |
|---|---|---|---|---|---|---|
| none (no techniques) | 7 | 71% | 254,385 |  |  | 64% |
| all | 7 | 71% | 114,362 |  |  | 45% |
| no-context | 7 | 57% | 177,170 | +62,808 | 45% | 46% |
| no-filememory | 7 | 71% | 101,623 | -12,740 | -9% | 34% |
| no-smalledits | 7 | 71% | 81,478 | -32,884 | -23% | 32% |
| no-testcompress | 7 | 100% | 57,421 | -56,941 | -41% | 28% |
| no-trim | 7 | 71% | 109,906 | -4,456 | -3% | 49% |
| no-pretest | 7 | 86% | 78,784 | -35,579 | -25% | 36% |
| no-stopongreen | 7 | 71% | 170,139 | +55,777 | 40% | 40% |

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

- trim on (`all`): 114,362 tokens per solved task, 45% of input tokens served from the provider's prompt cache
- trim off (`no-trim`): 109,906 (-4%), cached share 49%
- Cached tokens are cheaper than fresh ones, so compare cost per solved task as well as tokens: see the main table. If the provider reports no cached tokens at all (cached share 0%), the data cannot favour either side.


### Set B (2 paired runs per mode)

| mode | runs | pass rate | tokens / solved | extra tokens without it | share of savings | cached input share |
|---|---|---|---|---|---|---|
| none (no techniques) | 2 | 100% | 118,961 |  |  | 41% |
| all | 2 | 50% | 164,813 |  |  | 47% |
| no-context | 2 | 50% | 55,720 | -109,093 | 238% | 4% |
| no-filememory | 2 | 50% | 104,068 | -60,745 | 132% | 49% |
| no-smalledits | 2 | 100% | 51,506 | -113,307 | 247% | 46% |
| no-testcompress | 2 | 50% | 110,345 | -54,468 | 119% | 26% |
| no-trim | 2 | 50% | 124,805 | -40,008 | 87% | 49% |
| no-pretest | 2 | 100% | 113,122 | -51,692 | 113% | 60% |
| no-stopongreen | 2 | 100% | 159,798 | -5,014 | 11% | 61% |

**Set B, only the 1 task every mode above solved.** One failed run can spend 30 calls and decide the table above on its own; here pass/fail is the same for every mode, so the difference is what the technique saves (or costs) when the task gets done. *vs all*: how many more tokens the same tasks took with that technique off (negative = fewer).

| mode | total tokens | avg calls | vs all |
|---|---|---|---|
| none | 97,485 | 14.0 | -23% |
| all | 126,539 | 13.0 | - |
| no-context | 32,750 | 8.0 | -74% |
| no-filememory | 66,130 | 7.0 | -48% |
| no-smalledits | 65,373 | 7.0 | -48% |
| no-testcompress | 71,385 | 7.0 | -44% |
| no-trim | 79,474 | 8.0 | -37% |
| no-pretest | 75,183 | 8.0 | -41% |
| no-stopongreen | 192,789 | 19.0 | +52% |

**Trim versus the provider's prompt cache (set B).** Provider-reported cached input tokens decide this; the default is left as is.

- trim on (`all`): 164,813 tokens per solved task, 47% of input tokens served from the provider's prompt cache
- trim off (`no-trim`): 124,805 (-24%), cached share 49%
- Cached tokens are cheaper than fresh ones, so compare cost per solved task as well as tokens: see the main table. If the provider reports no cached tokens at all (cached share 0%), the data cannot favour either side.


## Excluded runs

These runs failed on infrastructure, not on the task. They stay in the results file, marked `infra_error`, and are in no average. A later retry of the same run, if it finished, is counted instead.

| reason | runs |
|---|---|
| quota | 3 |
| bad_request | 1 |

## Methodology

- **Models.** Both agents use the same model through the same Bifrost gateway: `gemini/gemini-3.1-flash-lite`. Gateway: `http://127.0.0.1:8180/v1`; virtual-key header `x-bf-vk`. Cascade tiers: off / off.
- **Who answered.** Models the gateway reported as serving the calls: `gemini-3.1-flash-lite`. Calls answered by a fallback provider (recorded per call for pinch; for mini-swe-agent only when its response carries it): 0. The benchmark key has no fallback chain, so a different model should never answer silently.
- **Pacing and rate limits.** Both agents are paced the same way: at least 4.5 s between model requests inside each agent process (`--min-call-interval`; pinch through `PINCH_MIN_CALL_INTERVAL`, mini-swe-agent through a wrapper around its model call that changes nothing else), and both wait out a per-minute HTTP 429 (the provider's "retry in N s", at most 120 s, up to 8 times) instead of failing. Only a per-day limit stops a run. This report: 0 rate-limit waits, 1,710 s spent waiting in total. **Times in the tables exclude those waits.** Pacing is per process, so with `--jobs` above 1 the runs share one limit and the pacing no longer keeps each agent under it.
- **Agents.** pinch (this repository, `--auto-approve`) versus mini-swe-agent `2.4.6` (MIT, SWE-agent team) with litellm `1.108.3`, its default tool-calling config (`mini.yaml`), no changes except the connection. Each run: at most 30 model calls, 4096 output tokens per call, 900 s wall time, for both agents. pinch tool mode: `lazy`.
- **Tasks.** Set A: Python exercises from Aider's polyglot benchmark (7 used in this run; Exercism, MIT; pinned commit 7e0611e, `bench/tasks/A/SOURCE.md`). Set B: real upstream bug fixes (8 used in this run; repository, fix commit and licence per task in `bench/tasks/B/SOURCE.md`; the starting tree is the commit before the fix, the fix's tests are the hidden check). Set C: docs tasks **written by us** (0 used in this run; `bench/tasks/C/SOURCE.md`).
- **How pass is checked.** Not by the agent. After an agent stops, the harness restores the exercise's tests (A), or adds the upstream fix's tests the agent never saw (B), or adds our check script (C), deletes cached bytecode, and runs the check in a fresh container. Pass = exit code 0.
- **Sandbox.** Every agent run and every check happens in a throwaway container of the pinned image `python:3.12.15-slim-bookworm` with the task folder mounted and host networking (needed to reach the gateway). Model-generated commands never run on the host. Limitation: the container has internet access, so an agent could in principle fetch information from the web.
- **Auto-approval.** pinch ran with `--auto-approve`, so writes, shell commands and git operations were approved without a person. Every one is logged in the run's session file (`auto_approved` events): 1069 in the 94 pinch runs in this report. mini-swe-agent has no approval gate in this configuration. This is a benchmark-only setting; pinch's normal mode asks first.
- **Tokens and cost.** Token counts are the `usage` the provider reported, relayed by Bifrost; nothing is estimated from text length. Cost = those tokens times `src/pinch/prices.toml`, the same table for both agents. A model without a price shows `n/a`.
- **Cache.** Real runs use a fresh Bifrost cache namespace each, so there are no cross-run cache hits. The 'same task twice' table is a separate demonstration.
- **Docs tool.** The Tavily `docs_lookup` tool is offered only on set C; on sets A and B `all` means every other technique (and `cascade` only when --small-model and --big-model are given). The techniques actually on for each run are recorded in its row (`techniques_on`).
- **Excluded.** Runs that failed on infrastructure (rate limit that did not clear, outage, exhausted quota or budget, bad credentials, harness error) are kept in the file with `status = infra_error` and left out of every number: 4 in this report. Rate limits are waited out and retried before a run is given up.
- **Limits.** One sample per cell unless `--repeat` was raised; models are not deterministic, so small differences are noise. `none` keeps pinch's default style (brief); the style table measures style on its own.
