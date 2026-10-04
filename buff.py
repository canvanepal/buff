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
  buff link <from> <to>    wire one sandbox so it can ssh into another
  buff link ls [from]      list links configured inside a sandbox
  buff link unlink <from> <to>   remove a link (config entry + key)
  buff swarm up|ls|run|down  orchestrate several sandboxes as an agent mesh

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


def sandbox_id(host):
    """'7681-idtgli4wgl5e24mehup90.e2b.app' -> 'idtgli4wgl5e24mehup90'."""
    sid = host.split(".")[0]
    if "-" in sid and sid.split("-", 1)[0].isdigit():
        sid = sid.split("-", 1)[1]
    return sid


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


async def is_live(host):
    """Quiet liveness probe: can we open the ttyd socket and get shell output?"""
    import websockets

    token = await fetch_token(host)
    try:
        async with websockets.connect("wss://" + host + WS_PATH, subprotocols=["tty"],
                                      max_size=None, ping_interval=None,
                                      compression=None, open_timeout=12) as ws:
            await ws.send(json.dumps({"AuthToken": token, "columns": 80, "rows": 24}).encode())
            for _ in range(4):
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                if isinstance(msg, bytes) and msg[:1] == b"0":
                    return True
            return True
    except Exception:
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


# ---------------- cross-sandbox linking (A -> B over the ws bridge) ----------------
# E2B sandboxes are isolated VMs with no private network between them, so the only
# path from A to B is A -> wss://8081-<B>.e2b.app -> B's websocat -> B's sshd.
# We give A its own keypair, authorize that key on B, and drop an ~/.ssh/config
# entry into A whose ProxyCommand does the websocket hop.

LINK_PREP_A = (
    "mkdir -p /root/.ssh && chmod 700 /root/.ssh; "
    "command -v ssh >/dev/null 2>&1 && command -v ssh-keygen >/dev/null 2>&1 "
    "|| { apt-get update -qq && apt-get install -y -qq openssh-client; }; "
    "if ! command -v websocat >/dev/null 2>&1; then "
    "curl -fsSL -o /usr/local/bin/websocat "
    "https://github.com/vi/websocat/releases/latest/download/websocat.x86_64-unknown-linux-musl "
    "&& chmod +x /usr/local/bin/websocat; fi; "
    "test -f /root/.ssh/buff_link "
    "|| ssh-keygen -t ed25519 -N '' -q -f /root/.ssh/buff_link; "
    "chmod 600 /root/.ssh/buff_link; "
    "cat /root/.ssh/buff_link.pub; echo A_READY"
)

LINK_ADD_KEY_B = (
    "mkdir -p /root/.ssh && chmod 700 /root/.ssh; "
    "touch /root/.ssh/authorized_keys; "
    "grep -qsF '__PUBKEY__' /root/.ssh/authorized_keys "
    "|| echo '__PUBKEY__' >> /root/.ssh/authorized_keys; "
    "chmod 600 /root/.ssh/authorized_keys; echo B_KEY_OK"
)

LINK_CONFIG_A = (
    "mkdir -p /root/.ssh && chmod 700 /root/.ssh; "
    "touch /root/.ssh/config /root/.ssh/known_hosts; "
    "chmod 600 /root/.ssh/config /root/.ssh/known_hosts; "
    "grep -q '^Host __ALIAS__$' /root/.ssh/config || printf '\\nHost __ALIAS__\\n"
    "  HostName __SID__\\n"
    "  User root\\n"
    "  IdentityFile /root/.ssh/buff_link\\n"
    "  StrictHostKeyChecking accept-new\\n"
    "  UserKnownHostsFile /root/.ssh/known_hosts\\n"
    "  ServerAliveInterval 15\\n"
    "  ProxyCommand /usr/local/bin/websocat --binary -B 65536 - wss://8081-__SID__.e2b.app\\n'"
    ">> /root/.ssh/config; "
    "echo CFG_OK"
)

LINK_LS_A = "cat /root/.ssh/config 2>/dev/null; echo LSDLIST_DONE"

LINK_UNLINK_A = (
    "test -f /root/.ssh/config || { echo NOCONFIG; exit 0; }; "
    "cp /root/.ssh/config /root/.ssh/config.bak; "
    "awk 'BEGIN{skip=0} /^Host __ALIAS__$/ {skip=1; next} /^Host / {skip=0} !skip' "
    "/root/.ssh/config > /root/.ssh/config.new "
    "&& mv /root/.ssh/config.new /root/.ssh/config; "
    "chmod 600 /root/.ssh/config; "
    "grep -q '^Host __ALIAS__$' /root/.ssh/config && echo STILL_THERE || echo UNLINK_OK"
)

LINK_REVOKE_B = (
    "test -f /root/.ssh/authorized_keys || { echo NOKEYFILE; exit 0; }; "
    "grep -vF '__PUBKEY__' /root/.ssh/authorized_keys > /root/.ssh/ak.new || true; "
    "cat /root/.ssh/ak.new > /root/.ssh/authorized_keys; "
    "rm -f /root/.ssh/ak.new; chmod 600 /root/.ssh/authorized_keys; echo REVOKE_OK"
)

LINK_PUBKEY_A = "cat /root/.ssh/buff_link.pub 2>/dev/null; echo PUBKEY_DONE"

LINK_RE = re.compile(r"wss://8081-([A-Za-z0-9_-]+)\.e2b\.app")


def parse_link_config(text):
    """Pull (alias, target-sandbox-id) pairs out of a sandbox ssh_config."""
    links, alias = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Host "):
            alias = line.split(None, 1)[1].strip()
            continue
        m = LINK_RE.search(line)
        if m and alias:
            links.append((alias, m.group(1)))
            alias = None
    return links


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
    sid = sandbox_id(host)
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


async def cmd_link(from_arg, to_arg, flags):
    """Wire sandbox <from_arg> so it can ssh into sandbox <to_arg>."""
    src_name, src = resolve_target([from_arg])
    dst_name, dst = resolve_target([to_arg])
    if src == dst:
        sys.exit("source and target are the same sandbox")
    src_id, dst_id = sandbox_id(src), sandbox_id(dst)
    alias = "buff-" + dst_id[:8]

    print("* preparing source sandbox " + src, file=sys.stderr)
    rc, out = await run_remote(src, LINK_PREP_A, timeout=300)
    if "A_READY" not in out:
        print(out[-2000:], file=sys.stderr)
        sys.exit("source preparation failed (rc=%d)" % rc)
    pubkey = ""
    for line in out.splitlines():
        if line.strip().startswith("ssh-ed25519"):
            pubkey = line.strip()
            break
    if not pubkey:
        print(out[-2000:], file=sys.stderr)
        sys.exit("could not read the source sandbox link key")

    print("* ensuring target sandbox has an ssh bridge: " + dst, file=sys.stderr)
    rc, out = await run_remote(dst, CHECK_SSH, timeout=30)
    if rc != 0:
        rc, out = await run_remote(dst, SETUP_SSH, timeout=300)
        if "SSH_READY" not in out:
            print(out[-2000:], file=sys.stderr)
            sys.exit("target provisioning failed (rc=%d)" % rc)

    print("* authorizing the source key on the target", file=sys.stderr)
    rc, out = await run_remote(dst, LINK_ADD_KEY_B.replace("__PUBKEY__", pubkey), timeout=60)
    if "B_KEY_OK" not in out:
        print(out[-2000:], file=sys.stderr)
        sys.exit("could not authorize the source key (rc=%d)" % rc)

    print("* writing ssh config in the source sandbox", file=sys.stderr)
    cfg = LINK_CONFIG_A.replace("__ALIAS__", alias).replace("__SID__", dst_id)
    rc, out = await run_remote(src, cfg, timeout=60)
    if "CFG_OK" not in out:
        print(out[-2000:], file=sys.stderr)
        sys.exit("could not write the source ssh config (rc=%d)" % rc)

    print("* verifying " + src_name + " -> " + dst_name, file=sys.stderr)
    check = "ssh -o BatchMode=yes " + alias + " 'echo LINK_OK; hostname'"
    rc, out = await run_remote(src, check, timeout=90)
    if "LINK_OK" in out:
        print("OK: " + src_name + " can ssh into " + dst_name + " as  ssh " + alias)
    else:
        print(out[-2000:], file=sys.stderr)
        sys.exit("link verification failed (rc=%d)" % rc)


async def cmd_link_ls(src_arg=None):
    """List the sandbox->sandbox links configured inside a sandbox."""
    if src_arg:
        name, host = resolve_target([src_arg])
    else:
        name, host = resolve_target(None)
    rc, out = await run_remote(host, LINK_LS_A, timeout=60)
    links = parse_link_config(out)
    names = {}
    for saved_name, saved_host in load_config().get("sandboxes", {}).items():
        names[sandbox_id(saved_host)] = saved_name
    if not links:
        print("no links in " + name + " (" + host + ")")
        return
    print("links in " + name + " (" + host + "):")
    for alias, target_id in links:
        label = names.get(target_id)
        print("  %-18s -> %s (%s)" % (alias, label if label else "?", target_id))


async def cmd_link_unlink(from_arg, to_arg):
    """Remove a sandbox->sandbox link: config entry in A + key on B."""
    src_name, src = resolve_target([from_arg])
    dst_name, dst = resolve_target([to_arg])
    if src == dst:
        sys.exit("source and target are the same sandbox")
    alias = "buff-" + sandbox_id(dst)[:8]

    print("* removing the ssh config entry in " + src, file=sys.stderr)
    rc, out = await run_remote(src, LINK_UNLINK_A.replace("__ALIAS__", alias), timeout=60)
    if "STILL_THERE" in out:
        sys.exit("could not remove the ssh config entry (rc=%d)" % rc)

    rc, out = await run_remote(src, LINK_PUBKEY_A, timeout=60)
    pubkey = ""
    for line in out.splitlines():
        if line.strip().startswith("ssh-ed25519"):
            pubkey = line.strip()
            break
    if not pubkey:
        print("OK: removed " + src_name + " -> " + dst_name + " (was: ssh " + alias + ")")
        print("warning: could not read the link key; a stale key may remain on "
              + dst_name, file=sys.stderr)
        return

    revoked = False
    try:
        rc, out = await run_remote(dst, LINK_REVOKE_B.replace("__PUBKEY__", pubkey),
                                   timeout=60)
        revoked = "REVOKE_OK" in out
    except Exception:
        revoked = False  # target may be expired/offline; config removal still stands

    print("OK: removed " + src_name + " -> " + dst_name + " (was: ssh " + alias + ")")
    if not revoked:
        print("warning: could not revoke the key on " + dst_name
              + " (sandbox may be offline) - re-run unlink once it is back", file=sys.stderr)


# ---------------- swarm: orchestrate sandboxes as an agent mesh ----------------
# A swarm is one hub sandbox that can ssh into every other sandbox (built with
# buff link), plus a registry file inside the hub describing the mesh. Tasks are
# fanned out from the hub to the workers and results come back the same way, so
# a whole fleet of agents works in parallel while only the hub stays online.

SWARM_REGISTRY = "/root/.buff/registry.json"


def parse_tasks(text):
    """Pull task lines out of a markdown/plain list: '- x', '* x', '1. x'."""
    tasks = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for prefix in ("- ", "* ", "+ "):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        else:
            m = re.match(r"^\d+[.)]\s+(.*)$", line)
            if m:
                line = m.group(1).strip()
        if line:
            tasks.append(line)
    return tasks


async def write_registry(hub_host, reg):
    import base64

    payload = base64.b64encode(json.dumps(reg, indent=2).encode()).decode()
    rc, out = await run_remote(hub_host,
                               "mkdir -p /root/.buff && echo " + payload + " | base64 -d > "
                               + SWARM_REGISTRY + "; echo REGISTRY_OK", timeout=60)
    return "REGISTRY_OK" in out


async def read_registry(hub_host):
    import uuid

    tag = uuid.uuid4().hex[:8]
    rc, out = await run_remote(hub_host,
                               "cat " + SWARM_REGISTRY + " 2>/dev/null; echo " + tag + "_x",
                               timeout=60)
    if tag + "_x" not in out:
        return None
    text = out.split(tag + "_x")[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


async def remote_upload(host, data, path, timeout=60):
    """base64-upload bytes to a file inside a sandbox (chunked, newline-safe)."""
    import base64

    b64 = base64.b64encode(data).decode()
    chunk = 1500  # multiple of 4 so base64 -d never sees a partial group
    for i in range(0, len(b64), chunk):
        part = b64[i:i + chunk]
        op = ">" if i == 0 else ">>"
        rc, out = await run_remote(host, "echo " + part + " | base64 -d " + op + " " + path
                                   + "; echo CHUNK_OK", timeout=timeout)
        if "CHUNK_OK" not in out:
            raise RuntimeError("upload to " + path + " failed on the sandbox")


async def swarm_up(hub_arg=None):
    cfg = load_config()
    if not cfg.get("sandboxes"):
        sys.exit("no saved sandboxes -- use: buff add <name> <host>")
    hub_name, hub_host = resolve_target([hub_arg] if hub_arg else None)
    members = []
    for name, host in cfg.get("sandboxes", {}).items():
        if host == hub_host:
            continue
        if not await is_live(host):
            print("  skip (offline): " + name, file=sys.stderr)
            continue
        print("* linking " + hub_name + " -> " + name, file=sys.stderr)
        try:
            await cmd_link(hub_name, name, {})
        except SystemExit:
            print("  link failed: " + name, file=sys.stderr)
            continue
        members.append({"name": name, "host": host, "id": sandbox_id(host),
                        "alias": "buff-" + sandbox_id(host)[:8]})
    reg = {"hub": {"name": hub_name, "host": hub_host, "id": sandbox_id(hub_host)},
           "members": members}
    if not await write_registry(hub_host, reg):
        sys.exit("could not write the registry inside " + hub_name)
    print("OK: hub " + hub_name + " with " + str(len(members)) + " member(s)")
    for m in members:
        print("  " + m["name"] + " -> " + m["alias"])


async def swarm_ls(hub_arg=None):
    hub_name, hub_host = resolve_target([hub_arg] if hub_arg else None)
    reg = await read_registry(hub_host)
    if not reg:
        print("no swarm registry in " + hub_name + " -- run: buff swarm up")
        return
    members = reg.get("members", [])
    print("swarm hub: " + reg["hub"]["name"] + " (" + reg["hub"]["host"] + ")")
    if not members:
        print("  no members")
        return
    status = {}
    script = ("for a in " + " ".join(m["alias"] for m in members) + "; do "
              "ssh -o BatchMode=yes -o ConnectTimeout=6 $a true >/dev/null 2>&1 "
              "&& echo PING_OK $a || echo PING_FAIL $a; done")
    rc, out = await run_remote(hub_host, script, timeout=180)
    for line in out.splitlines():
        for key in ("PING_OK ", "PING_FAIL "):
            if key in line:
                status[line.split(key)[1].strip()] = key.strip()
    for m in members:
        print("  %-10s %-18s %-9s (%s)" % (m["name"], m["alias"],
                                          status.get(m["alias"], "?"), m["id"]))


async def swarm_run(tasks_arg, flags):
    import base64

    hub_arg = flags.get("hub")
    hub_name, hub_host = resolve_target([hub_arg] if hub_arg else None)
    reg = await read_registry(hub_host)
    if not reg:
        sys.exit("no swarm registry in " + hub_name + " -- run: buff swarm up")
    members = reg.get("members", [])
    if not members:
        sys.exit("the swarm has no members")
    if tasks_arg == "-":
        text = sys.stdin.read()
    else:
        p = Path(tasks_arg)
        if not p.exists():
            sys.exit("no such task file: " + tasks_arg)
        text = p.read_text(encoding="utf-8", errors="replace")
    tasks = parse_tasks(text)
    if not tasks:
        sys.exit("no tasks found (use '- ', '* ' or '1. ' lines)")
    repo = flags.get("repo", "")
    cmd = flags.get("cmd", "")
    timeout = int(flags.get("timeout") or 900)
    ws = "hub-" + re.sub(r"[^A-Za-z0-9]+", "-", hub_name).strip("-")

    buckets = {m["alias"]: [] for m in members}
    for i, t in enumerate(tasks):
        buckets[members[i % len(members)]["alias"]].append((i, t))

    failed = 0
    for m in members:
        items = buckets[m["alias"]]
        if not items:
            continue
        lines = ["set +e", 'WS=/root/swarm/' + ws,
                 'mkdir -p "$WS/tasks" "$WS/logs"', 'cd "$WS"']
        if repo:
            lines.append('if [ -d repo/.git ]; then git -C repo pull --ff-only; '
                         'else git clone "' + repo + '" repo; fi')
        for i, t in items:
            tb = base64.b64encode(t.encode()).decode()
            lines.append('echo ' + tb + ' | base64 -d > "$WS/tasks/' + str(i) + '.md"')
            if cmd:
                inner = base64.b64encode(
                    ("echo " + tb + " | base64 -d > /tmp/swarm_task.txt").encode()).decode()
                lines.append("echo " + inner + " | base64 -d | bash")
                run = cmd.replace("{task}", '"$(cat /tmp/swarm_task.txt)"')
                # wrap in a subshell so capturing the log cannot override a
                # redirection inside the user's own command
                lines.append("( " + run + " ) > \"$WS/logs/" + str(i) + '.log" 2>&1')
            lines.append("echo TASK_RESULT " + str(i) + " $?")
        script_path = "/tmp/swarm_run.sh"
        await remote_upload(hub_host, ("\n".join(lines) + "\n").encode(), script_path)
        print("* " + m["name"] + ": " + str(len(items)) + " task(s)", file=sys.stderr)
        rc, out = await run_remote(hub_host,
                                   "ssh -o BatchMode=yes " + m["alias"] + " bash -s < "
                                   + script_path + " 2>&1; echo MEMBER_DONE", timeout=timeout)
        results = {}
        for line in out.splitlines():
            if "TASK_RESULT" in line:
                parts = line.split("TASK_RESULT")[1].split()
                if len(parts) >= 2:
                    try:
                        results[int(parts[0])] = int(parts[1])
                    except ValueError:
                        pass
        if "MEMBER_DONE" not in out:
            print("  warning: " + m["name"] + " did not finish (link may be down)",
                  file=sys.stderr)
        for i, _t in items:
            rc_i = results.get(i)
            if rc_i == 0:
                print("  " + m["name"] + " task " + str(i) + ": ok")
            else:
                failed += 1
                print("  " + m["name"] + " task " + str(i) + ": FAILED"
                      + (" (rc=" + str(rc_i) + ")" if rc_i is not None else ""))
                if cmd:
                    print("    fetch logs with: buff ssh " + hub_name
                          + " -- \"ssh " + m["alias"] + " cat /root/swarm/" + ws
                          + "/logs/" + str(i) + ".log\"")
                else:
                    print("    task file: " + m["alias"] + ":/root/swarm/" + ws
                          + "/tasks/" + str(i) + ".md")
    print(str(len(tasks)) + " task(s) dispatched to " + str(len(members)) + " member(s), "
          + str(failed) + " failed")
    sys.exit(1 if failed else 0)


async def swarm_down(hub_arg=None):
    hub_name, hub_host = resolve_target([hub_arg] if hub_arg else None)
    reg = await read_registry(hub_host)
    if not reg:
        sys.exit("no swarm registry in " + hub_name)
    for m in reg.get("members", []):
        print("* unlinking " + hub_name + " -> " + m["name"], file=sys.stderr)
        await cmd_link_unlink(hub_name, m["name"])
    rc, out = await run_remote(hub_host, "rm -f " + SWARM_REGISTRY + "; echo REGISTRY_GONE",
                               timeout=60)
    print("OK: swarm dismantled")


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

    if cmd == "link":
        flags, rest = split_flags(argv[1:])
        sub = rest[0] if rest else None
        if sub in ("ls", "list"):
            await cmd_link_ls(rest[1] if len(rest) > 1 else None)
            return
        if sub in ("rm", "unlink"):
            if len(rest) != 3:
                sys.exit("usage: buff link unlink <from> <to>")
            await cmd_link_unlink(rest[1], rest[2])
            return
        if len(rest) != 2:
            sys.exit("usage: buff link <from> <to>\n"
                     "       buff link ls [from]\n"
                     "       buff link unlink <from> <to>")
        await cmd_link(rest[0], rest[1], flags)
        return

    if cmd == "swarm":
        flags, rest = split_flags(argv[1:])
        sub = rest[0] if rest else "ls"
        hub = flags.get("hub")
        if sub == "up":
            await swarm_up(hub)
        elif sub == "ls":
            await swarm_ls(hub)
        elif sub == "run":
            await swarm_run(rest[1] if len(rest) > 1 else "-", flags)
        elif sub in ("down", "rm"):
            await swarm_down(hub)
        else:
            sys.exit("usage: buff swarm up|ls|down [--hub NAME]\n"
                     "       buff swarm run <tasks-file|-> [--hub NAME] [--repo URL] "
                     "[--cmd 'agent cmd {task}'] [--timeout S]")
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
        elif a in ("--token", "--cols", "--rows", "--hub", "--repo", "--cmd",
                   "--timeout") and i + 1 < len(argv):
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
