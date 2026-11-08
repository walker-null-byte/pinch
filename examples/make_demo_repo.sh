#!/bin/sh
# Make a tiny git repo with one bug (add() subtracts). Usage: examples/make_demo_repo.sh /tmp/demo-repo
set -e
dir="${1:?usage: make_demo_repo.sh DIR}"
mkdir -p "$dir" && cd "$dir" && git init -q
printf 'def add(a, b):\n    return a - b\n' > calc.py
printf 'from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n' > test_calc.py
git add . && git -c user.email=demo@example.com -c user.name=demo commit -qm init
echo "demo repo ready in $dir"
