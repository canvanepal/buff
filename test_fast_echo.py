#!/usr/bin/env python3
"""Unit tests for FastEcho predictive local echo."""
import io
import sys
import time

sys.path.insert(0, ".")
from ttyd_bridge import FastEcho, ANSI_CSI, ANSI_OSC, PROMPT_TAIL


def make(quiet=True):
    s = FastEcho(fast=True)
    s.rendered = io.BytesIO()
    s.out = s.rendered
    # simulate a root prompt arrival: colored "~ # " + bracketed paste on
    s.on_output(b"\x1b[?2004h\x1b[32m~\x1b[0m \x1b[37m#\x1b[0m ")
    if quiet:
        s.last_output = time.monotonic() - 1.0  # pretend output went quiet
    return s


def t(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


ok = True

# 1. prompt detection from colored tail
s = make()
ok &= t("prompt detected on colored '~ # ' tail", s.at_prompt())

# 2. printable prediction renders and queues suppression
s = make()
ret = s.key(b"l")
ok &= t("key returns data unchanged", ret == b"l")
ok &= t("rendered 'l' locally", s.rendered.getvalue() == b"l")
ok &= t("suppress queue has 1 entry", len(s.suppress) == 1)
s.key(b"s")
disp = s.on_output(b"ls")  # remote echo of both chars
ok &= t("echo of 'ls' fully swallowed", disp == b"" and len(s.suppress) == 0)

# 3. enter: render CRLF, hold disarms, echo suppressed
s = make()
s.key(b"l"); s.key(b"s")
ret = s.key(b"\r")
ok &= t("enter rendered CRLF", s.rendered.getvalue() == b"ls\r\n")
ok &= t("enter sets hold", s.hold)
disp = s.on_output(b"ls\r\n")  # command echo
ok &= t("enter echo suppressed", disp == b"")
ok &= t("hold released by next output", not s.hold)
# after command output + NEW prompt arrives, prediction rearms
s.on_output(b"total 0\r\n")
s.on_output(b"\x1b[32m~\x1b[0m \x1b[37m#\x1b[0m ")
s.last_output = time.monotonic() - 1.0
ok &= t("rearmed at new prompt", s.at_prompt())

# 4. backspace: renders erase, suppresses remote \\b \\b
s = make()
s.key(b"a"); s.key(b"b")
s.key(b"\x7f")
ok &= t("backspace rendered erase", s.rendered.getvalue() == b"ab\b \b")
disp = s.on_output(b"ab\b \b")
ok &= t("backspace echo suppressed", disp == b"")

# 5. mismatch: divergence clears queue and passes data through
s = make()
s.key(b"x")
disp = s.on_output(b"COMPLETELY DIFFERENT")
ok &= t("mismatch passes through + clears queue", disp == b"COMPLETELY DIFFERENT" and not s.suppress)

# 6. partial chunk boundary: echo split across chunks
s = make()
s.key(b"a"); s.key(b"b")
d1 = s.on_output(b"a")     # half the echo
d2 = s.on_output(b"b")     # rest
ok &= t("split echo fully swallowed", d1 == b"" and d2 == b"")

# 7. TTL expiry: stale expectation is dropped
s = make()
s.key(b"z")
exp, _ = s.suppress[0]
s.suppress[0] = [exp, time.monotonic() - 0.1]  # force expiry
disp = s.on_output(b"unrelated")
ok &= t("expired entry dropped, output passes", disp == b"unrelated" and not s.suppress)

# 8. TUI: alt-screen disables prediction, exit re-enables
s = make()
s.on_output(b"\x1b[?1049h")  # vim enters
s.last_output = time.monotonic() - 1.0
ok &= t("tui detected", s.tui)
ret = s.key(b"i")
ok &= t("no prediction in tui", s.rendered.getvalue() == b"" and ret == b"i")
s.on_output(b"\x1b[?1049l")  # vim exits
s.last_output = time.monotonic() - 1.0
ok &= t("tui cleared on exit", not s.tui)

# 9. paste: big burst not predicted
s = make()
s.key(b"x" * 40)
ok &= t("paste bypasses prediction", s.rendered.getvalue() == b"")

# 10. fast=False: pure passthrough
s = FastEcho(fast=False)
out = s.on_output(b"\x1b[32m~ # ")
ok &= t("fast=False passthrough", out == b"\x1b[32m~ # ")
ret = s.key(b"a")
ok &= t("fast=False no render", ret == b"a" and not s.suppress)

# 11. control keys disarm instead of predicting
s = make()
s.key(b"\t")
ok &= t("tab disarms without render", s.rendered.getvalue() == b"")

print()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
