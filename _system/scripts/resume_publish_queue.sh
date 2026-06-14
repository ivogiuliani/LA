#!/bin/bash
# Resume the remaining IG publish queue (run when the 24h quota frees up).
cd "$(dirname "$0")/../.." || exit 1
python3 - << 'PY'
import json, subprocess, sys, time
from pathlib import Path
Q = Path("_system/social/migration/publish_queue_remaining.json")
remaining = json.loads(Q.read_text())
done = []
for fname in list(remaining):
    r = subprocess.run([sys.executable, "_system/scripts/publish_instagram.py",
                        "--publish-live", fname], capture_output=True, text=True, timeout=300)
    link = next((ln.split("→",1)[1].strip() for ln in r.stdout.splitlines() if "PUBLISHED →" in ln), "")
    ok = r.returncode == 0
    print(f"{'✓' if ok else '✗'} {fname[24:70]} {link}")
    if not ok:
        if "too many actions" in r.stdout + r.stderr or "2207042" in r.stdout + r.stderr:
            print("  quota still exhausted — stopping, re-run later")
            break
        print(r.stdout[-200:])
        break
    remaining.remove(fname)
    Q.write_text(json.dumps(remaining, indent=2))
    time.sleep(10)
print(f"\nremaining in queue: {len(remaining)}")
PY
