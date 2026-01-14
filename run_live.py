#!/usr/bin/env python3
import subprocess
import time
from datetime import datetime

from db_paths import spots_db_path

FAST_EVERY_SEC = 60
ENRICH_EVERY_SEC = 10 * 60  # 10 minutes

def run(cmd: list[str]) -> int:
    t0 = time.time()
    print(f"\n[{datetime.now().isoformat(timespec='seconds')}] $ {' '.join(cmd)}")
    p = subprocess.run(cmd)
    dt = time.time() - t0
    print(f"[done] rc={p.returncode} elapsed={dt:.1f}s")
    return p.returncode

def main():
    last_enrich = 0.0

    while True:
        loop_start = time.time()

        # FAST LOOP (keep “now” live)
        run(["python3", "run_pota_refresh.py", ])
        run(["python3", "build_pota_edges.py",  "--minutes", "60", "--limit", "20000"])
        run(["python3", "make_park_status.py", "--window-min", "30", "--min-edges", "1"])

        # SLOW LOOP (cache warming)
        if loop_start - last_enrich >= ENRICH_EVERY_SEC:
            run(["python3", "qrz_enrich_spotters.py", "--limit", "400"])
            last_enrich = loop_start

        # Sleep to hit cadence
        elapsed = time.time() - loop_start
        sleep_for = max(0.0, FAST_EVERY_SEC - elapsed)
        print(f"[sleep] {sleep_for:.1f}s")
        time.sleep(sleep_for)

if __name__ == "__main__":
    main()
