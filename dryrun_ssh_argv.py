"""Dry-run `buff ssh <name> --tmux` for a given buff.py copy.

Prints the exact argv cmd_ssh would exec. No network, no subprocess:
run_ssh (new) / subprocess.call (old) are replaced with a capture stub.
Usage: python dryrun_ssh_argv.py <path-to-buff.py>
"""
import asyncio
import importlib.util
import subprocess
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("buff_dryrun", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

captured = {}

if hasattr(mod, "run_ssh"):           # new code
    def fake_run_ssh(argv, reconnect=True, **kw):
        captured["argv"] = list(argv)
        captured["reconnect"] = reconnect
        return 0
    mod.run_ssh = fake_run_ssh
else:                                 # old code (subprocess.call inside cmd_ssh)
    def fake_call(argv, *a, **kw):
        captured["argv"] = list(argv)
        return 0
    subprocess.call = fake_call

sys.argv = ["buff", "ssh", "2", "--tmux", "--no-setup"]
try:
    asyncio.run(mod.main_async())
except SystemExit:
    pass

argv = captured.get("argv")
if not argv:
    print(path, "-> cmd_ssh never reached exec (no argv captured)")
    sys.exit(1)
# show everything after the options: locate the destination token
print(path)
for i, a in enumerate(argv):
    mark = "  <-- DESTINATION" if a.startswith("root@") else ""
    print("  argv[%d] = %r%s" % (i, a, mark))
# the bug: tokens that start with '-' appear AFTER ssh parsed options but
# BEFORE the destination -> ssh rejects them as unknown options
dest = next(i for i, a in enumerate(argv) if a.startswith("root@"))
bad = [a for i, a in enumerate(argv) if a.startswith("--") and i < dest]
print("PRE-DESTINATION LONG OPTS:", bad if bad else "none")
