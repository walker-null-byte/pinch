# Set B: real bug fixes

Each task is a real upstream bug fix. `repo/` is the project one commit before the fix (equivalently: the fix commit with its
source change reverted). The fix commit's tests are the hidden check. Prompts are written by us from the upstream commit message and tests.

| Task | Project | Licence | Fix commit | Parent commit |
|---|---|---|---|---|
| `boltons-backoff-constant-factor` | [boltons](https://github.com/mahmoud/boltons) | BSD-3-Clause | [`ead236e278`](https://github.com/mahmoud/boltons/commit/ead236e278ca0466bf468de746b5960fb12d7e5b) (2026-07-17) | `57cb026b7f` |
| `humanize-intword-rounding-carry` | [humanize](https://github.com/python-humanize/humanize) | MIT | [`ca892b368a`](https://github.com/python-humanize/humanize/commit/ca892b368a535a79cc5e2181d052245b94fd6652) (2026-09-16) | `f971127ce7` |
| `more-itertools-bucket-falsey-validator` | [more-itertools](https://github.com/more-itertools/more-itertools) | MIT | [`67fcf64047`](https://github.com/more-itertools/more-itertools/commit/67fcf640477c78605580f5ee703a54c73c1e7a8a) (2026-10-06) | `1ea82a711c` |
| `cachetools-tlru-overwrite-expired` | [cachetools](https://github.com/tkem/cachetools) | MIT | [`c0fdf6abab`](https://github.com/tkem/cachetools/commit/c0fdf6abab38040947d6fe2e38c507401d5e2350) (2026-07-23) | `978d34d40c` |
| `parse-tt-matches-empty` | [parse](https://github.com/r1chardj0n3s/parse) | MIT | [`52bf0b3862`](https://github.com/r1chardj0n3s/parse/commit/52bf0b3862c6e21271e9582a3a98915247228763) (2026-10-27) | `cc86df74f5` |
| `python-dotenv-empty-value-inline-comment` | [python-dotenv](https://github.com/theskumar/python-dotenv) | BSD-3-Clause | [`f5485a61ee`](https://github.com/theskumar/python-dotenv/commit/f5485a61eefa5e686d6d5bdc7aa9ad6b104b1e92) (2026-08-17) | `49515afee2` |
| `xmltodict-unparse-none-values` | [xmltodict](https://github.com/martinblech/xmltodict) | MIT | [`aa165113be`](https://github.com/martinblech/xmltodict/commit/aa165113bef2b3a1a822209863343b9dc9ffe43a) (2026-02-15) | `f7d76c96fc` |
| `toolz-interpose-empty` | [toolz](https://github.com/pytoolz/toolz) | BSD-3-Clause | [`d2eba0374a`](https://github.com/pytoolz/toolz/commit/d2eba0374a452c2b18fe188b04527e22bda3548c) (2026-09-18) | `55ce42dc3b` |

The upstream LICENSE file of each project is kept at the top of its `repo/` folder. Built by `scripts/build_b_tasks.py`.
