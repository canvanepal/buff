#!/usr/bin/env python3
"""
buff -- connect to your Freebuff cloud sandbox terminal from your own terminal.

Commands:
  buff                 connect to the last-used sandbox (or the only one saved)
  buff [--fast] <name> connect to a named saved sandbox (--fast = predictive echo)
  buff add <name> <host>   save a sandbox host (e.g. 7681-abcd123.e2b.app)
  buff ls              list saved sandboxes
  buff rm <name>       remove a saved sandbox
  buff test [name]     handshake-check a sandbox without entering it
  buff ssh [name]      real SSH session into the sandbox (auto-provisions sshd)
  buff ssh-setup [name]    provision sshd + websocat bridge without connecting

Options:
  --fast               mosh-style predictive local echo (instant keystrokes;
                       auto-disabled in full-screen apps like vim)
  --no-setup           skip sandbox ssh provisioning check
  --token TOKEN        override the auth token (fetched fresh by default)
  --cols N --rows M    terminal size override

The token is always fetched fresh from https://<host>/token at connect time.
Type Ctrl+] to leave the session.
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".buff"
CONFIG_FILE = CONFIG_DIR / "sandboxes.json"

WS_PATH = "/ws"


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"sandboxes": {}, "last": None}


def save_config(cfg):
    CONFIG_DIR.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def resolve_target(args):
    cfg = load_config()
    sandboxes = cfg.get("sandboxes", {})

    if not args:  # plain `buff`
        if cfg.get("last") and cfg["last"] in sandboxes:
            return cfg["last"], sandboxes[cfg["last"]]
        if len(sandboxes) == 1:
            name = next(iter(sandboxes))
            return name, sandboxes[name]
        if sandboxes:
            print("Multiple sandboxes saved. Pick one:")
            for name in sandboxes:
                print("  buff", name)
            sys.exit(1)
        print("No sandboxes saved yet.")
        print("Get the host from freebuff.com (open the project terminal, copy the")
        print("*.e2b.app host from the page URL), then run:")
        print("  buff add mybox 7681-xxxxxxxx.e2b.app")
        sys.exit(1)

    name = args[0]
    if name in sandboxes:
        return name, sandboxes[name]

    # not a saved name -> treat the argument itself as a host or full URL
    host = name
    for prefix in ("https://", "wss://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.split("/")[0]
    if ".e2b.app" not in host and "localhost" not in host and "127.0.0.1" not in host:
        print(name, "is not a saved sandbox and does not look like a sandbox host.")
        sys.exit(1)
    return host, host


async def fetch_token(host):
    """GET https://<host>/token -> token string (may be empty)."""
    import urllib.request

    url = "https://" + host + "/token"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode()).get("token", "")
    except Exception as e:
        print("warning: could not fetch token:", e, file=sys.stderr)
        return ""


async def cmd_test(host):
    import websockets

    token = await fetch_token(host)
    url = "wss://" + host + WS_PATH
    try:
        async with websockets.connect(url, subprotocols=["tty"], max_size=None,
                                      ping_interval=None, compression=None,
                                      open_timeout=10) as ws:
            await ws.send(json.dumps({"AuthToken": token, "columns": 80, "rows": 24}).encode())
            for _ in range(4):
                msg = await asyncio.wait_for(ws.recv(), timeout=4)
                if isinstance(msg, bytes) and msg[:1] == b"0":
                    print("OK", host, "is live (got shell output)")
                    return True
            print("OK", host, "answered but sent no output")
            return True
    except Exception as e:
        print("FAIL", host, "->", type(e).__name__, str(e))
        return False


# ---------------- real SSH over the e2b websocket bridge ----------------
# Same architecture as E2B's official docs: sshd runs inside the sandbox; a
# websocat process bridges ws://0.0.0.0:8081 -> tcp://127.0.0.1:22 inside the
# sandbox; the local ssh client tunnels through it with a ProxyCommand.

CHECK_SSH = (
    "pgrep -x sshd >/dev/null 2>&1 "
    "&& pgrep -f ws-l:0.0.0.0:8081 >/dev/null 2>&1 "
    "&& test -s /root/.ssh/authorized_keys"
)

SETUP_SSH = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "command -v sshd >/dev/null 2>&1 "
    "|| { apt-get update -qq && apt-get install -y -qq openssh-server; }; "
    "if ! command -v websocat >/dev/null 2>&1; then "
    "curl -fsSL -o /usr/local/bin/websocat "
    "https://github.com/vi/websocat/releases/latest/download/websocat.x86_64-unknown-linux-musl "
    "&& chmod +x /usr/local/bin/websocat; fi; "
    "mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh; "
    "grep -qsF '__PUBKEY__' /root/.ssh/authorized_keys 2>/dev/null "
    "|| echo '__PUBKEY__' >> /root/.ssh/authorized_keys; "
    "chmod 600 /root/.ssh/authorized_keys; "
    "pgrep -x sshd >/dev/null 2>&1 || /usr/sbin/sshd; "
    "pgrep -f ws-l:0.0.0.0:8081 >/dev/null 2>&1 "
    "|| nohup /usr/local/bin/websocat -b ws-l:0.0.0.0:8081 tcp:127.0.0.1:22 "
    ">/tmp/ws8081.log 2>&1 & sleep 0.5; echo SSH_READY"
)

WEBSOCAT_WIN_URL = (
    "https://github.com/vi/websocat/releases/latest/download/"
    "websocat.x86_64-pc-windows-gnu.exe"
)


async def run_remote(host, script, timeout=300):
    """Run a bash script inside the sandbox through the ttyd websocket.
    Returns (returncode, output). Uses base64 so nothing in the script can be
    confused with terminal echo, and a random marker to detect completion."""
    import base64
    import uuid
    import websockets

    token = await fetch_token(host)
    url = "wss://" + host + WS_PATH
    tag = uuid.uuid4().hex[:8]
    payload = base64.b64encode(script.encode()).decode()
    line = "echo " + payload + " | base64 -d | bash; echo " + tag + "_$?"
    pat = re.compile((tag + r"_(\d+)").encode())
    buf = b""
    async with websockets.connect(url, subprotocols=["tty"], max_size=None,
                                  ping_interval=None, compression=None,
                                  open_timeout=15) as ws:
        await ws.send(json.dumps({"AuthToken": token, "columns": 200, "rows": 50}).encode())
        await ws.send(b'1{"columns":200,"rows":50}')
        await ws.send(b"0" + line.encode() + b"\r")
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("remote command timed out after " + str(timeout) + "s")
            msg = await asyncio.wait_for(ws.recv(), timeout=left)
            if isinstance(msg, bytes) and msg[:1] == b"0":
                buf += msg[1:]
                m = pat.search(buf)
                if m:
                    return int(m.group(1)), buf.decode("utf-8", "replace")


def ensure_local_key():
    """Return the local SSH public key, generating an ed25519 key if needed."""
    import subprocess
    ssh_dir = Path.home() / ".ssh"
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        p = ssh_dir / name
        if p.exists():
            return p.read_text().strip()
    ssh_dir.mkdir(exist_ok=True)
    key = ssh_dir / "id_ed25519"
    print("* generating local ssh key " + str(key), file=sys.stderr)
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)], check=True)
    return key.with_suffix(".pub").read_text().strip()


def ensure_local_websocat():
    """Local websocat binary path (downloads a Windows build if missing)."""
    import shutil
    import urllib.request
    if sys.platform != "win32":
        w = shutil.which("websocat")
        if not w:
            sys.exit("install websocat locally first (e.g. brew install websocat)")
        return w
    dest = CONFIG_DIR / "websocat.exe"
    if dest.exists() and dest.stat().st_size > 1000000:
        return str(dest)
    print("* downloading local websocat.exe (one-time)...", file=sys.stderr)
    dest.parent.mkdir(exist_ok=True)
    urllib.request.urlretrieve(WEBSOCAT_WIN_URL, dest)
    return str(dest)


async def cmd_ssh(host, flags, setup_only=False, remote_cmd=None):
    import shutil
    import subprocess

    if shutil.which("ssh") is None:
        sys.exit("OpenSSH client not found. Install Windows OpenSSH or Git for Windows.")

    pubkey = ensure_local_key()
    ensure_local_websocat()  # local tunnel binary (also used by the session below)
    if not flags.get("no-setup"):
        rc, out = await run_remote(host, CHECK_SSH, timeout=30)
        if rc != 0:
            print("* provisioning sshd + websocat inside sandbox (one-time)...", file=sys.stderr)
            script = SETUP_SSH.replace("__PUBKEY__", pubkey)
            rc, out = await run_remote(host, script, timeout=300)
            if "SSH_READY" not in out:
                print(out[-2000:], file=sys.stderr)
                sys.exit("ssh provisioning failed (rc=%d)" % rc)
    if setup_only:
        print("OK: sshd is up in", host)
        return

    # host looks like 7681-<sandboxid>.e2b.app -> bare sandbox id
    sid = host.split(".")[0]
    if "-" in sid and sid.split("-", 1)[0].isdigit():
        sid = sid.split("-", 1)[1]
    wsocat = ensure_local_websocat()
    proxy = (wsocat.replace("\\", "/") + " --binary -B 65536 - wss://8081-"
             + sid + ".e2b.app")
    argv = ["ssh",
            "-o", "ProxyCommand=" + proxy,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=" + str(CONFIG_DIR / "known_hosts"),
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "root@" + sid]
    if remote_cmd:
        argv.extend(remote_cmd)
    print("* ssh root@" + sid + "  (real SSH over the e2b websocket bridge)", file=sys.stderr)
    rc = subprocess.call(argv)
    sys.exit(rc)


async def main_async():
    # lazy import so `buff ls/rm` works even if websockets is missing
    sys.path.insert(0, str(Path(__file__).parent))
    from ttyd_bridge import run_session

    argv = sys.argv[1:]
    cmd = argv[0] if argv else ""

    if cmd == "add":
        if len(argv) != 3:
            sys.exit("usage: buff add <name> <host>")
        cfg = load_config()
        cfg.setdefault("sandboxes", {})[argv[1]] = argv[2]
        cfg["last"] = argv[1]
        save_config(cfg)
        print("saved:", argv[1], "->", argv[2], "(now the default)")
        return

    if cmd == "ls":
        cfg = load_config()
        for name, host in cfg.get("sandboxes", {}).items():
            marker = "  (default)" if name == cfg.get("last") else ""
            print("%-20s %s%s" % (name, host, marker))
        if not cfg.get("sandboxes"):
            print("no sandboxes saved -- use: buff add <name> <host>")
        return

    if cmd == "rm":
        if len(argv) != 2:
            sys.exit("usage: buff rm <name>")
        cfg = load_config()
        if cfg.get("sandboxes", {}).pop(argv[1], None) is None:
            sys.exit("no sandbox named " + argv[1])
        if cfg.get("last") == argv[1]:
            cfg["last"] = next(iter(cfg["sandboxes"]), None)
        save_config(cfg)
        print("removed:", argv[1])
        return

    if cmd in ("ssh", "ssh-setup"):
        flags, rest = split_flags(argv[1:])
        name, host = resolve_target(rest or None)
        # anything after the target (optionally separated by --) is the remote command
        tail = rest[1:] if rest else []
        if tail and tail[0] == "--":
            tail = tail[1:]
        await cmd_ssh(host, flags, setup_only=(cmd == "ssh-setup"), remote_cmd=tail)
        return

    if cmd == "test":
        target = argv[1:] or None
        name, host = resolve_target(target)
        ok = await cmd_test(host)
        sys.exit(0 if ok else 1)

    # `buff` or `buff <name|host>` -> connect
    flags, rest = split_flags(argv)
    extra = [] if not rest else [rest[0]]
    name, host = resolve_target(extra)
    print("* connecting to", host, "... (Ctrl+] to exit)", file=sys.stderr)
    if not flags.get("fast"):
        print("  tip: use 'buff --fast' for instant keystroke echo on slow links", file=sys.stderr)
    token = await fetch_token(host)
    url = "wss://" + host + WS_PATH
    cfg = load_config()
    if name in cfg.get("sandboxes", {}):
        cfg["last"] = name
        save_config(cfg)
    try:
        await run_session(url, token=token, fast=flags.get("fast", False))
    except Exception as e:
        print()
        print("* session ended:", type(e).__name__, str(e), file=sys.stderr)


def split_flags(argv):
    """Pull --fast / --token X / --cols N / --rows N out of argv."""
    flags, rest, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--fast":
            flags["fast"] = True
            i += 1
        elif a == "--no-setup":
            flags["no-setup"] = True
            i += 1
        elif a in ("--token", "--cols", "--rows") and i + 1 < len(argv):
            flags[a[2:]] = argv[i + 1]
            i += 2
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            rest.append(a)
            i += 1
    return flags, rest


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print()
        print("* bye", file=sys.stderr)


if __name__ == "__main__":
    main()
