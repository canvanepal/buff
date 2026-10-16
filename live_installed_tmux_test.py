"""End-to-end: the INSTALLED ~/.buff/buff.py, its own dispatch, its own argv.

Loads the installed copy, lets main_async build the ssh argv for
`buff ssh <target> --tmux` (run_ssh stubbed to capture instead of exec),
then executes that captured argv for real: tmux must attach, `exit` must
give rc=0, and no stray 'buff' session may be left behind.
Usage: python live_installed_tmux_test.py [target-name]
"""
import asyncio
import importlib.util
import subprocess
import sys
import time

sys.path.insert(0, ".")
import buff  # repo copy, only for run_remote / config helpers

TARGET = sys.argv[1] if len(sys.argv) > 1 else "main"
INSTALLED = "C:/Users/Arun/.buff/buff.py"

spec = importlib.util.spec_from_file_location("buff_installed", INSTALLED)
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)

# capture the argv instead of running ssh
captured = {}
inst.run_ssh = lambda argv, reconnect=True, **kw: (
    captured.setdefault("argv", list(argv)) or 0)

sys.argv = ["buff", "ssh", TARGET, "--tmux", "--no-setup"]
try:
    asyncio.run(inst.main_async())
except SystemExit:
    pass

argv = captured.get("argv")
if not argv:
    print("FAIL: installed copy never built an argv")
    sys.exit(1)

name, host = buff.resolve_target([TARGET])
dest = next((a for a in argv if a.startswith("root@")), None)
print("captured argv from INSTALLED copy:")
print("  destination:", dest)
print("  has -tt:", "-tt" in argv)
print("  tmux cmd after dest:",
      bool(dest and argv.index(dest) < len(argv) - 1
           and "tmux new -A" in argv[-1]))
bad = [a for a in argv[:argv.index(dest)] if a.startswith("--")]
print("  stray long opts before dest:", bad if bad else "none")

# safety: don't hijack an existing session on the target box
rc, out = asyncio.run(buff.run_remote(host, "tmux ls 2>&1; echo TMUXRC_$?",
                                      timeout=30))
if "buff" in out and "TMUXRC_0" in out:
    print("SKIP live attach: a 'buff' tmux session already exists on", host)
    sys.exit(0)

# run the captured argv for real
p = subprocess.Popen(argv, stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(6)
if p.poll() is not None:
    early = p.stdout.read() or b""
    print("FAIL: ssh exited early rc=%s: %r" % (p.returncode, early[-600:]))
    sys.exit(1)
try:
    out_bytes, _ = p.communicate(b"exit\r", timeout=30)
    rc_ssh = p.returncode
except subprocess.TimeoutExpired:
    p.kill()
    out_bytes, _ = p.communicate()
    rc_ssh = -1
buf = out_bytes or b""
print("LIVE attach (tmux bar seen):", "YES" if b"buff" in buf else "NO")
print("LIVE clean exit rc=0:", "YES" if rc_ssh == 0 else "NO (rc=%d)" % rc_ssh)

rc, out = asyncio.run(buff.run_remote(host, "tmux ls 2>&1; echo TMUXRC_$?",
                                      timeout=30))
leftover = "buff" in out and "TMUXRC_0" in out
print("LIVE no stray session:", "YES" if not leftover else "NO -> " + repr(out))

ok = (b"buff" in buf) and rc_ssh == 0 and not leftover
print()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
