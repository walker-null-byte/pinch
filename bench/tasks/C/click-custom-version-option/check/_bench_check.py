import sys
from click.testing import CliRunner
import tool

def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)

runner = CliRunner()
for value, expected in (("/etc/tool.toml", "tool 1.4.2 (config: /etc/tool.toml)\n"), ("/other.toml", "tool 1.4.2 (config: /other.toml)\n")):
    res = runner.invoke(tool.cli, ["--version"], prog_name="tool", env={"TOOL_CONFIG": value})
    if res.output != expected:
        fail(f"with TOOL_CONFIG={value}: got {res.output!r}, expected {expected!r} (exit {res.exit_code})")
res = runner.invoke(tool.cli, ["--version"], prog_name="tool", env={"TOOL_CONFIG": None})
if res.output != "tool 1.4.2 (config: default)\n":
    fail(f"without TOOL_CONFIG: got {res.output!r}")
res = runner.invoke(tool.cli, ["hello"], prog_name="tool")
if res.output != "hello\n":
    fail(f"hello changed: {res.output!r}")
used = [p for p in tool.cli.params if p.callback is not None and "custom_version_option" in p.callback.__qualname__]
if not used:
    fail("the --version option was not made with click.custom_version_option")
print("PASS")
