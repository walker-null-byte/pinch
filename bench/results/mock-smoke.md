# Benchmark report (MOCK): mock-smoke.jsonl

> **MOCK DATA.** Every number below comes from a scripted stand-in for a model (`pinch.bench.mock`), served through the real Bifrost gateway. It proves the harness records, scores and summarises runs correctly. It says NOTHING about how well pinch or mini-swe-agent perform with a real model.

Model: `mock-small` · counted runs: 18 · excluded (infrastructure): 0 · sets: A, B

## Results per set and mode (MOCK)

Tokens are provider-reported `usage` (input + output) through Bifrost. *Tokens per solved task* = all tokens the group spent, failed runs included, divided by the runs that passed. *vs baseline* compares that figure with mini-swe-agent's.

### Set A: 2 tasks (MOCK)

| mode | runs | solved | avg tokens | median tokens | tokens / solved | vs baseline | cost / solved | avg calls | median time |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 2 | 1 (50%) | 10,556 | 10,556 | 21,112 | - | $0.0250 | 5.0 | 6s |
| none | 2 | 1 (50%) | 10,299 | 10,299 | 20,598 | -2% | $0.0235 | 4.0 | 2s |
| all | 2 | 1 (50%) | 6,150 | 6,150 | 12,299 | -42% | $0.0145 | 2.0 | 2s |
| no-pretest | 2 | 1 (50%) | 8,576 | 8,576 | 17,151 | -19% | $0.0198 | 3.0 | 2s |

### Set B: 1 task (MOCK)

| mode | runs | solved | avg tokens | median tokens | tokens / solved | vs baseline | cost / solved | avg calls | median time |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 1 | 1 (100%) | 16,993 | 16,993 | 16,993 | - | $0.0185 | 5.0 | 6s |
| none | 1 | 1 (100%) | 17,219 | 17,219 | 17,219 | +1% | $0.0185 | 5.0 | 3s |
| all | 1 | 1 (100%) | 26,533 | 26,533 | 26,533 | +56% | $0.0276 | 4.0 | 4s |
| no-pretest | 1 | 1 (100%) | 34,104 | 34,104 | 34,104 | +101% | $0.0354 | 5.0 | 4s |

## Ablation: what each technique contributes (MOCK)

`all` minus one technique, compared with `all` on the same tasks. *Extra tokens without it* = tokens per solved task of `no-<technique>` minus that of `all`. *Share of savings* = that extra divided by (`none` minus `all`): how much of the total saving the technique accounts for. Negative = the technique cost tokens here. Shares of techniques that overlap do not add up to 100%. A technique that changes nothing in a run (for example `cache` when no request repeats) shows ~0.

### Set A (2 paired runs per mode) (MOCK)

| mode | runs | pass rate | tokens / solved | extra tokens without it | share of savings | cached input share |
|---|---|---|---|---|---|---|
| none (no techniques) | 2 | 50% | 20,598 |  |  | 0% |
| all | 2 | 50% | 12,299 |  |  | 0% |
| no-pretest | 2 | 50% | 17,151 | +4,852 | 58% | 0% |

### Set B (1 paired runs per mode) (MOCK)

| mode | runs | pass rate | tokens / solved | extra tokens without it | share of savings | cached input share |
|---|---|---|---|---|---|---|
| none (no techniques) | 1 | 100% | 17,219 |  |  | 0% |
| all | 1 | 100% | 26,533 |  |  | 0% |
| no-pretest | 1 | 100% | 34,104 | +7,571 | -81% | 0% |

## Same task twice, in one cache namespace (MOCK)

A separate demonstration, NOT part of the averages above: each task is run twice in a row with the same Bifrost cache namespace, so the second run can be answered from the cache. Calls after a tool result that differs between the two runs (a test run prints its own timing) miss and are billed, so the second run is cheaper, not free. The real runs above use a fresh namespace per run and never share answers.

|  | runs | avg billed tokens | avg calls | cache hits | tokens not billed | avg time |
|---|---|---|---|---|---|---|
| run 1 (cold) | 3 | 12,944 | 2.7 | 0 | 0 | 3s |
| run 2 (same task again) | 3 | 2,536 | 2.7 | 7 | 31,223 | 3s |

## Excluded runs (MOCK)

None.

## Methodology

- **Models.** Both agents use the same model through the same Bifrost gateway: `mock-small` (a scripted mock model: MOCK). Gateway: `http://127.0.0.1:8190/v1`; virtual-key header `x-bf-vk`. Cascade tiers: off / off.
- **Who answered.** Models the gateway reported as serving the calls: `mock-small`. Calls answered by a fallback provider (recorded per call for pinch; for mini-swe-agent only when its response carries it): 0. The benchmark key has no fallback chain, so a different model should never answer silently.
- **Agents.** pinch (this repository, `--auto-approve`) versus mini-swe-agent `2.4.6` (MIT, SWE-agent team) with litellm `1.108.3`, its default tool-calling config (`mini.yaml`), no changes except the connection. Each run: at most 12 model calls, 1024 output tokens per call, 240 s wall time, for both agents. pinch tool mode: `lazy`.
- **Tasks.** Set A: Python exercises from Aider's polyglot benchmark (2 used in this run; Exercism, MIT; pinned commit 7e0611e, `bench/tasks/A/SOURCE.md`). Set B: real upstream bug fixes (1 used in this run; repository, fix commit and licence per task in `bench/tasks/B/SOURCE.md`; the starting tree is the commit before the fix, the fix's tests are the hidden check). Set C: docs tasks **written by us** (not part of this run; `bench/tasks/C/SOURCE.md`).
- **How pass is checked.** Not by the agent. After an agent stops, the harness restores the exercise's tests (A), or adds the upstream fix's tests the agent never saw (B), or adds our check script (C), deletes cached bytecode, and runs the check in a fresh container. Pass = exit code 0.
- **Sandbox.** Every agent run and every check happens in a throwaway container of the pinned image `python:3.12.15-slim-bookworm` with the task folder mounted and host networking (needed to reach the gateway). Model-generated commands never run on the host. Limitation: the container has internet access, so an agent could in principle fetch information from the web.
- **Auto-approval.** pinch ran with `--auto-approve`, so writes, shell commands and git operations were approved without a person. Every one is logged in the run's session file (`auto_approved` events): 39 in the 15 pinch runs in this report. mini-swe-agent has no approval gate in this configuration. This is a benchmark-only setting; pinch's normal mode asks first.
- **Tokens and cost.** Token counts are the `usage` the provider reported, relayed by Bifrost; nothing is estimated from text length. Cost = those tokens times `src/pinch/prices.toml`, the same table for both agents. A model without a price shows `n/a`.
- **Cache.** Real runs use a fresh Bifrost cache namespace each, so there are no cross-run cache hits. The 'same task twice' table is a separate demonstration.
- **Docs tool.** The Tavily `docs_lookup` tool is offered only on set C; on sets A and B `all` means every other technique (and `cascade` only when --small-model and --big-model are given). The techniques actually on for each run are recorded in its row (`techniques_on`).
- **Excluded.** Runs that failed on infrastructure (rate limit that did not clear, outage, exhausted quota or budget, bad credentials, harness error) are kept in the file with `status = infra_error` and left out of every number: 0 in this report. Rate limits are waited out and retried before a run is given up.
- **Limits.** One sample per cell unless `--repeat` was raised; models are not deterministic, so small differences are noise. `none` keeps pinch's default style (brief); the style table measures style on its own.
