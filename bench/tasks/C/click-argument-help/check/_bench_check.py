import sys
from click.testing import CliRunner
import cli

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

res = CliRunner().invoke(cli.greet, ["--help"])
out = res.output
if res.exit_code != 0:
    fail(f"--help exited {res.exit_code}")
if "Positional arguments" not in out:
    fail("--help has no 'Positional arguments' section:\n" + out)
tail = out.split("Positional arguments", 1)[1]
for text in ("Who to greet.", "How many times to greet."):
    if text not in tail:
        fail(f"{text!r} is not listed under 'Positional arguments':\n" + out)
helps = {p.name: getattr(p, "help", None) for p in cli.greet.params if p.param_type_name == "argument"}
if helps != {"name": "Who to greet.", "count": "How many times to greet."}:
    fail(f"argument help attributes are {helps}")
if "Greet NAME, COUNT times." not in out or "--shout" not in out:
    fail("the rest of the help output changed")
run = CliRunner().invoke(cli.greet, ["Ann", "2", "--shout"])
if run.output != "HELLO, ANN!\nHELLO, ANN!\n":
    fail(f"behaviour changed: {run.output!r}")
print("PASS")
