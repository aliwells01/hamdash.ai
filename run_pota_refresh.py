#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
import sqlite3
from pathlib import Path

# ---------- EDIT THESE PATHS ONCE ----------
PROJECT_DIR = Path("/Users/aw/Documents/Hobbies/Ham/ham-ui")  # where ssb_agent.py + scripts live
from db_paths import spots_db_path
ACTIVE_JSON = PROJECT_DIR / "ssb_picks.rows.json"  # produced by ssb_agent.py
# ------------------------------------------

def log(msg: str):
    print(msg, flush=True)

def run(cmd: list[str], cwd: Path | None = None):
    log(f"[run] {' '.join(cmd)}")
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if p.returncode != 0:
        raise SystemExit(f"Command failed (exit {p.returncode}): {' '.join(cmd)}")

def db_scalar(sql: str, params=()):
    with sqlite3.connect(spots_db_path()) as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None

def iso_to_dt(s: str | None):
    if not s:
        return None
    try:
        # supports ...Z
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def main():
    ap = argparse.ArgumentParser(description="Run the full Radio Intel pipeline (one command).")
    ap.add_argument("--skip-ssb-agent", action="store_true", help="Skip ssb_agent.py (if you already have active list another way)")
    ap.add_argument("--history-limit", type=int, default=100, help="How many active POTA activations to fetch history for")
    ap.add_argument("--edges-limit", type=int, default=8000, help="How many edges to build per run")
    ap.add_argument("--qrz-limit", type=int, default=80, help="How many new spotter calls to QRZ-enrich per run")
    ap.add_argument("--qrz-every-min", type=int, default=30, help="Only run QRZ enrichment if last run older than this many minutes")
    ap.add_argument("--freshness-min", type=int, default=10, help="Consider DB 'fresh' if last history fetch within this many minutes")
    ap.add_argument("--db", help="SQLite database path")
    args = ap.parse_args()

    # Basic sanity
    if not DB_PATH.exists():
        raise SystemExit(f"DB not found: {DB_PATH}")
    if not PROJECT_DIR.exists():
        raise SystemExit(f"Project dir not found: {PROJECT_DIR}")

    start = datetime.now(timezone.utc)
    log(f"[info] DB: {DB_PATH}")
    log(f"[info] Project: {PROJECT_DIR}")

    # --- Step 0: quick freshness readout ---
    last_hist = db_scalar("SELECT MAX(fetched_at_utc) FROM pota_spot_history;")
    last_hist_dt = iso_to_dt(last_hist)
    if last_hist_dt:
        age_min = (start - last_hist_dt).total_seconds() / 60.0
        log(f"[info] last pota_spot_history fetch: {last_hist} ({age_min:.1f} min ago)")
    else:
        log("[info] last pota_spot_history fetch: <none>")

    # --- Step 1: ssb_agent (optional; used for active list JSON) ---
    if not args.skip_ssb_agent:
        # This refreshes ssb_picks.rows.json
        run([sys.executable, "ssb_agent.py"], cwd=PROJECT_DIR)

        if not ACTIVE_JSON.exists():
            raise SystemExit(f"Expected active JSON not found after ssb_agent run: {ACTIVE_JSON}")
        log(f"[ok] Active list updated: {ACTIVE_JSON.name}")

    # --- Step 2: fetch POTA histories into SQLite ---
    # You should already have pota_history.py in PROJECT_DIR; this uses ACTIVE_JSON for activations.
    # If in your setup the file has a different name, change it below.
    pota_history_py = PROJECT_DIR / "pota_history.py"
    if not pota_history_py.exists():
        raise SystemExit(f"Missing {pota_history_py}. If your file is named differently, edit run_radio_intel.py.")

    run([
        sys.executable, str(pota_history_py),
        "--db", str(DB_PATH),
        "--active-json", str(ACTIVE_JSON),
        "--limit", str(args.history_limit),
    ], cwd=PROJECT_DIR)

    # --- Step 3: QRZ enrichment (only occasionally) ---
    qrz_py = PROJECT_DIR / "qrz_enrich_spotters.py"
    if qrz_py.exists():
        last_qrz = db_scalar("SELECT MAX(updated_at_utc) FROM callsign_location;")
        last_qrz_dt = iso_to_dt(last_qrz)
        run_qrz = True
        if last_qrz_dt:
            age = (start - last_qrz_dt).total_seconds() / 60.0
            log(f"[info] last callsign_location update: {last_qrz} ({age:.1f} min ago)")
            if age < args.qrz_every_min:
                run_qrz = False
                log(f"[skip] QRZ enrichment (ran within last {args.qrz_every_min} minutes)")
        if run_qrz:
            run([
                sys.executable, str(qrz_py),
                "--db", str(DB_PATH),
                "--limit", str(args.qrz_limit),
            ], cwd=PROJECT_DIR)
    else:
        log("[warn] qrz_enrich_spotters.py not found; skipping QRZ enrichment")

    # --- Step 4: build edges (distance now; propagation later) ---
    edges_py = PROJECT_DIR / "build_pota_edges.py"
    if not edges_py.exists():
        raise SystemExit(f"Missing {edges_py}. If your file is named differently, edit run_radio_intel.py.")

    run([
        sys.executable, str(edges_py),
        "--db", str(DB_PATH),
        "--limit", str(args.edges_limit),
    ], cwd=PROJECT_DIR)

    # --- Step 5: print status summary ---
    hist_rows = db_scalar("SELECT COUNT(*) FROM pota_spot_history;")
    edges_rows = db_scalar("SELECT COUNT(*) FROM pota_heard_edges;")
    cached_calls = db_scalar("SELECT COUNT(*) FROM callsign_location;")
    last_hist2 = db_scalar("SELECT MAX(fetched_at_utc) FROM pota_spot_history;")
    log("")
    log("[status]")
    log(f"  pota_spot_history rows: {hist_rows}")
    log(f"  pota_heard_edges rows : {edges_rows}")
    log(f"  callsign_location rows: {cached_calls}")
    log(f"  last history fetch     : {last_hist2}")

    dur = (datetime.now(timezone.utc) - start).total_seconds()
    log(f"[done] pipeline completed in {dur:.1f}s")

if __name__ == "__main__":
    main()
