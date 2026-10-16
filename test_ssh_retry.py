#!/usr/bin/env python3
"""Tests for buff.run_ssh auto-reconnect (the `buff ssh` transport).

Uses injected call/sleep doubles so no real ssh or network is involved:
  1. bridge drops twice (rc=255) then succeeds -> retried, returns 0,
     with 1s then 2s backoff
  2. reconnect=False -> single attempt, no sleeping (--no-reconnect)
  3. non-255 exits (0, 1, 42) -> returned immediately, never retried
  4. Ctrl+C during the backoff wait -> returns 130 promptly
  5. backoff stays capped at 10s across many drops
"""
import sys

sys.path.insert(0, ".")
from buff import run_ssh


def t(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


def make_call(rcs):
    """Returns (call, calls) - call pops scripted return codes (last repeats)."""
    calls = []

    def call(argv):
        calls.append(list(argv))
        return rcs[len(calls) - 1] if len(calls) <= len(rcs) else rcs[-1]

    return call, calls


ok = True

# 1. two drops then success -> 3 attempts, rc 0, backoff 1s then 2s
calls_made, sleeps = [], []
call, calls = make_call([255, 255, 0])
rc = run_ssh(["ssh", "root@sid"], reconnect=True,
             call=call, sleep=sleeps.append)
ok &= t("drop x2 then success returns 0", rc == 0)
ok &= t("ssh attempted 3 times", len(calls) == 3)
ok &= t("backoff was 1s then 2s", sleeps == [1.0, 2.0])

# 2. reconnect=False -> single attempt, no sleeping
call, calls = make_call([255])
rc = run_ssh(["ssh", "root@sid"], reconnect=False,
             call=call, sleep=sleeps.append)
ok &= t("no-reconnect returns 255", rc == 255)
ok &= t("no-reconnect tries exactly once", len(calls) == 1)

# 3. non-255 exits stop the loop immediately
for code in (0, 1, 42):
    call, calls = make_call([code])
    rc = run_ssh(["ssh", "root@sid"], reconnect=True,
                 call=call, sleep=sleeps.append)
    ok &= t("exit %d returned as-is, no retry" % code,
            rc == code and len(calls) == 1)

# 4. Ctrl+C during backoff -> 130, promptly
def interrupting_sleep(delay):
    raise KeyboardInterrupt

call, calls = make_call([255])
rc = run_ssh(["ssh", "root@sid"], reconnect=True,
             call=call, sleep=interrupting_sleep)
ok &= t("Ctrl+C during backoff returns 130", rc == 130)
ok &= t("Ctrl+C stops after one attempt", len(calls) == 1)

# 5. backoff caps at 10s
call, calls = make_call([255] * 7 + [0])
sleeps = []
run_ssh(["ssh", "root@sid"], reconnect=True, call=call, sleep=sleeps.append)
expected = [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]
ok &= t("backoff caps at 10s", sleeps == expected)

# argv is passed through untouched on every attempt
call, calls = make_call([255, 7])
argv = ["ssh", "-o", "ProxyCommand=x", "-t", "root@sid", "exec tmux"]
run_ssh(argv, reconnect=True, call=call, sleep=lambda d: None)
ok &= t("argv unchanged across retries",
        all(c == argv for c in calls))

print()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
