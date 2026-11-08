# Set A: Aider polyglot benchmark, Python exercises

- What: the 34 Python exercises of [Aider-AI/polyglot-benchmark](https://github.com/Aider-AI/polyglot-benchmark)
- Pinned commit: `7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f` (2024-12-22)
- Origin and licence: the exercises are taken from the [Exercism Python track](https://github.com/exercism/python).
  Content is copyright (c) Exercism and released under the MIT licence (`LICENSE-exercism-python.txt`, copied from
  https://github.com/exercism/python/blob/main/LICENSE). The polyglot-benchmark repository has no licence file of its own; its README says
  the exercises "are used in accordance with Exercism's open source licenses" and credits Exercism. We do the same.
- Pass means: the exercise's own test file, restored to its pristine content after the agent has finished, passes under pytest
  in a fresh container.
- What we changed: nothing in the exercises. `repo/` is the exercise folder without `.meta/` and `.docs/` (the reference solution
  `.meta/example.py` is stored only in `gold/`, which no agent sees), and the prompt is the exercise's instructions plus a short tail.
- Built by `scripts/build_a_tasks.py`.
