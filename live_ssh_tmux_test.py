"""Live e2e for `buff ssh --tmux`.

Builds the exact argv cmd_ssh now produces for tmux mode, runs it against the
real box over the websocat bridge, feeds `exit` once the tmux prompt is up,
and expects a clean rc=0. Then verifies no stray 'buff' session is left.
"""
import subprocess
import sys
import time

sys.path.insert(0, ".")
import buff

HOST = sys.argv[1] if len(sys.argv) > 1 else "7681-iec7mdhobz4a9geme96o9.e2b.app"
SID = buff.sandbox_id(HOST)
wsocat = buff.ensure_local_websocat()
proxy = (wsocat.replace("\\", "/") + " --binary -B 65536 - wss://8081-" + SID + ".e2b.app")

argv = ["ssh",
        "-o", "ProxyCommand=" + proxy,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=" + str(buff.CONFIG_DIR / "known_hosts"),
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=20",
        "-tt",
        "root@" + SID,
        "if command -v tmux >/dev/null 2>&1; then "
        "exec tmux new -A -s buff; else exec bash; fi"]

print("running: ssh --tmux-mode session ...", flush=True)
t0 = time.monotonic()
p = subprocess.Popen(argv, stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
# collect output on a thread so we notice early ssh failures
import threading
chunks = []
th = threading.Thread(target=lambda: chunks.append(p.stdout.read() or b""),
                      daemon=True)
th.start()
time.sleep(6)
if p.poll() is not None:
    time.sleep(0.3)
    print("ssh exited early rc=%s: %r" % (p.returncode, b"".join(chunks)[-800:]))
    sys.exit(1)
# communicate() writes+closes stdin atomically (a bare flush() can hit
# EINVAL on Windows pipes once ssh has grabbed the pty)
try:
    out, _ = p.communicate(b"exit\r", timeout=30)
    rc = p.returncode
except subprocess.TimeoutExpired:
    p.kill()
    out, _ = p.communicate()
    rc = -1
th.join(timeout=2)
buf = (b"".join(chunks) + (out or b""))
wall = time.monotonic() - t0
saw_tmux = b"buff" in buf

# no stray session should remain (exit ends the shell -> session closes)
mc, out = __import__("asyncio").run(
    buff.run_remote(HOST, "tmux ls 2>&1; echo TMUXRC_$?", timeout=30))
leftover = "buff" in out and "TMUXRC_0" in out

print("LIVE ssh --tmux attach:", "YES" if saw_tmux else "NO -> " + repr(buf[-300:]))
print("LIVE clean exit rc=0:", "YES" if rc == 0 else "NO (rc=%d)" % rc)
print("LIVE no stray session after exit:", "YES" if not leftover else "NO -> " + repr(out))
print("(wall %.1fs)" % wall)
ok = saw_tmux and rc == 0 and not leftover
print()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
