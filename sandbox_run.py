"""Run a shell script inside a sandbox via buff.run_remote and print the output.

Usage: python sandbox_run.py <script-file> [host]
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import buff

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_HOST = "7681-idtgli4wgl5e24mehup90.e2b.app"


async def main():
    script_path = sys.argv[1]
    host = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_HOST
    script = Path(script_path).read_text()
    rc, out = await buff.run_remote(host, script, timeout=300)
    print(out)
    print("\n[remote rc=%d]" % rc)


asyncio.run(main())