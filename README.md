# buff — your freebuff.com cloud sandbox, from your own terminal

`buff` is a small Python CLI that connects to the **freebuff.com** cloud sandbox
(E2B VM running `ttyd`) straight from your local terminal — and, on top of that,
sets up **real SSH** into the same sandbox.

Everything here was reverse-engineered from a single Chrome DevTools HAR capture
of a freebuff.com session (the HAR itself is **not** in this repo — it contains
live session tokens; see [Security notes](#security-notes)).

---

## Why

freebuff.com gives you a browser terminal backed by an E2B sandbox. That works,
but it's a browser tab: no local shell tools, no ssh/scp, no editor integration,
and every keystroke pays a ~250 ms round trip through the E2B edge.

`buff` fixes that by speaking the same wire protocol the website does, directly,
from your terminal:

- `buff` — interactive shell in your terminal (with optional predictive local echo)
- `buff ssh` — **real OpenSSH** into the sandbox (`root@` shell, scp/rsync/vim all work)

---

## Quick start

```bash
# install (this repo)
pip install websockets          # only runtime dependency

# tell buff where your sandbox lives (host shown in the freebuff.com page URL)
buff add mybox 7681-<sandbox-id>.e2b.app

buff                # connect (last-used sandbox)
buff --fast         # connect with mosh-style predictive echo
buff ssh            # real SSH session (auto-provisions sshd on first use)
buff test           # handshake-check without entering
buff ls             # list saved sandboxes
```

Global install used during development:

```bash
cp buff.py ttyd_bridge.py ~/.buff/
ln -s ~/.buff/buff.py ~/bin/buff     # plus a buff.cmd wrapper on Windows
```

---

## Commands

| command | what it does |
|---|---|
| `buff` | connect to the last-used (or only) saved sandbox |
| `buff [--fast] <name\|host>` | connect to a named sandbox; `--fast` = predictive echo |
| `buff add <name> <host>` | save a sandbox host |
| `buff ls` / `buff rm <name>` | list / remove saved sandboxes |
| `buff test [name]` | protocol handshake check, no shell entered |
| `buff ssh [name] [-- cmd...]` | real SSH into the sandbox |
| `buff ssh-setup [name]` | provision sshd + ws bridge only, don't connect |
| `buff link <from> <to>` | wire one sandbox so it can `ssh` into another |
| `buff link ls [from]` | list the links configured inside a sandbox |
| `buff link unlink <from> <to>` | remove a link (config entry + authorized key) |

Flags: `--fast`, `--no-setup` (skip provisioning check), `--token X`, `--cols N`, `--rows M`.

Exit an interactive session with **Ctrl+]**.

---

## How it works

### 1. The ttyd websocket protocol (recovered from the HAR)

freebuff's page embeds a `ttyd` browser terminal. The protocol, decoded from the
page JS inside the capture:

- connect to `wss://<host>/ws` with websocket subprotocol **`tty`**
- **auth = a binary frame containing raw JSON** — no opcode prefix:

  ```json
  {"AuthToken":"<fetched from https://<host>/token>","columns":120,"rows":32}
  ```

  The server sniffs the leading `{` to tell it apart from framed messages
  (this was the key bug: a `J`-prefix guess fails silently).

- **server → client** binary frames, first byte is the type:
  - `'0'` OUTPUT — terminal bytes (append to the PTY stream)
  - `'1'` TITLE — window title
  - `'2'` PING — empty = keepalive echo; non-empty = preferences blob (theme etc., ignore)
- **client → server** binary frames:
  - `'0'` INPUT — keystrokes/bytes to the shell
  - `'1'` RESIZE + JSON, e.g. `1{"columns":200,"rows":50}`
  - `'2'` PING — keepalive

`/token` returns `{"token": ""}` — auth is effectively open (that's freebuff's
design, not a bug we introduced).

### 2. Interactive bridge (`ttyd_bridge.py`)

A raw-mode PTY bridge: local stdin → `'0'` INPUT frames, OUTPUT frames → stdout.
Handles Windows (msvcrt) and POSIX (termios/tioc) raw mode, window resizes, and
Ctrl+] exit.

**FastEcho** (used with `--fast`) is a mosh-style predictive local echo that
hides the ~250 ms edge latency:

- instantly echoes printable keys locally while the real echo is in flight
- detects prompts (ANSI-stripped line tail matching `[#>$%] ?$`) and holds echo there
- disarms in TUIs (`\x1b[?1049h`, `?25l`, mouse-tracking modes) and on paste >8 chars
- reconciles queued keystrokes against server output (split-packet safe)
- 22/22 unit tests pass (`test_fast_echo.py`)

### 3. Real SSH (`buff ssh`)

Same architecture as E2B's official SSH recipe, auto-provisioned through the
existing ttyd websocket so nothing extra needs to be exposed:

1. `buff` runs a base64-encoded bash script inside the sandbox over the ttyd ws
   (random marker + `$?` to detect completion reliably — terminal echo would
   otherwise garble plain-text matching)
2. the script installs `openssh-server`, appends your local ed25519 pubkey to
   `/root/.ssh/authorized_keys`, and starts `sshd`
3. it also starts `websocat -b ws-l:0.0.0.0:8081 tcp:127.0.0.1:22` inside the
   sandbox, bridging websocket → sshd
4. locally, ssh is invoked with:

   ```
   ProxyCommand  ~/.buff/websocat.exe --binary -B 65536 - wss://8081-<sandbox-id>.e2b.app
   ```

First run downloads `websocat.exe` (Windows) and generates `~/.ssh/id_ed25519`
if you have none. After `buff ssh-setup` once, `ssh`/`scp`/`rsync` work natively
against `root@<sandbox-id>` too.

---

## 4. Cross-sandbox linking (`buff link`)

E2B sandboxes are isolated VMs — there is no private network between them, so the
only path from sandbox A to sandbox B is out through the public websocket bridge:

```
A  ──ssh──►  A's websocat  ──wss──►  8081-<B>.e2b.app  ──►  B's websocat  ──►  B's sshd
```

`buff link <from> <to>` wires this up and verifies it:

1. prepares sandbox A — installs `openssh-client` + `websocat`, generates a
   **dedicated** `~/.ssh/buff_link` keypair (separate from any personal key, so
   the link is easy to revoke)
2. ensures B has its ssh bridge (`sshd` + `websocat` on 8081)
3. authorizes A's link key in B's `authorized_keys`
4. writes an `~/.ssh/config` entry into A with the `ProxyCommand` hop
5. runs a live `ssh` from A to B and fails loudly if it doesn't answer

After linking, you can `buff ssh <from>` and then simply:

```bash
ssh buff-<target-id-prefix>      # lands you inside the other sandbox
```

Liveness note: if freebuff reprovisions a sandbox its id changes, so re-run
`buff link` for that pair — the config entry is keyed on the target's id.

### Managing links

```bash
buff link ls                 # links inside the default sandbox
buff link ls mybox           # links inside a specific sandbox
buff link unlink mybox main  # remove the link
```

`ls` reads the sandbox's own `ssh_config` and resolves target ids back to your
saved sandbox names. `unlink` does a full teardown, not just a config edit:

1. strips the `Host` block from the source sandbox's `~/.ssh/config`
   (keeping a `config.bak` alongside it)
2. revokes the source's link key from the target's `authorized_keys`

If the target sandbox is expired or offline, the config removal still succeeds
and you get a warning telling you to re-run `unlink` once it's back — the link
is already unusable in the meantime, since the ssh alias no longer exists.

---

## Latency findings

Measured against a live sandbox (these motivated FastEcho):

| layer | latency |
|---|---|
| ICMP ping to E2B host | ~23 ms |
| websocket stack ping (local edge hop) | ~0.3 ms |
| HTTPS transaction to sandbox | ~400 ms |
| keystroke → echo on screen | ~250–260 ms steady (up to ~620 ms busy) |

The freebuff shell wrapper was A/B tested and ruled out (wrapped ~252 ms vs
plain bash ~315 ms — within noise). The delay is E2B's edge/proxy chain, so the
only real fix client-side is predictive echo.

---

## Repo layout

| file | purpose |
|---|---|
| `buff.py` | main CLI: config, connect, test, ssh provisioning |
| `ttyd_bridge.py` | PTY bridge + FastEcho predictive echo |
| `test_fast_echo.py` | 22 unit tests for the echo predictor |
| `ttyd_smoke_test.py` | protocol handshake smoke test against a live host |
| `latency_probe.py` / `latency_layers.py` / `latency_ab_test.py` | latency diagnostics used for the table above |

---

## Security notes

- **The original `freebuff.com.har` capture is deliberately NOT in this repo.**
  HAR files contain live credentials — in this case Convex JWTs and an OAuth
  authorization code. Anyone with the file could hijack the session. Never share
  a HAR unsanitized; if you need one for analysis, redact `authorization` headers
  and cookies first.
- `buff` fetches a fresh token from `https://<host>/token` at every connect; it
  is never persisted to disk.
- The sandbox's ttyd and `/token` endpoint are unauthenticated by freebuff's
  design — treat the sandbox host like a password and don't publish it.
- SSH keys are standard `~/.ssh/id_ed25519`; the pubkey is the only thing written
  into the sandbox.

## Requirements

- Python 3.10+ with `websockets`
- OpenSSH client (`ssh`) for `buff ssh`
- network access to `*.e2b.app`
