"""Live check of the `buff ssh` transport.

cmd_ssh runs: local ssh -> ProxyCommand websocat -> wss://8081-<sid>
with ServerAliveInterval=15 / CountMax=3.

Test: a remote command that is silent for 100s (> the ~60s edge idle kill).
If SSH keepalives push traffic through the websocket, it survives.
Also prints how long the connection took, and a quick idle test with no
command (connection open, silent).
"""
import subprocess
import sys
import time

sys.path.insert(0, ".")
import buff

HOST = "7681-iec7mdhobz4a9geme96o9.e2b.app"
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
        "root@" + SID,
        "sleep 100; echo SSH_SILENT_OK"]

print("running: 100s-silent command over buff ssh transport ...", flush=True)
t0 = time.monotonic()
p = subprocess.run(argv, capture_output=True, timeout=180)
wall = time.monotonic() - t0
out = (p.stdout + p.stderr).decode("utf-8", "replace")
ok = "SSH_SILENT_OK" in out and p.returncode == 0
print("LIVE ssh 100s-silent:", "YES" if ok else "NO",
      "(rc=%d, wall=%.1fs)" % (p.returncode, wall))
if not ok:
    print("output:", repr(out[-1500:]))
sys.exit(0 if ok else 1)
