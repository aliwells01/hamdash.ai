#!/usr/bin/env python3
"""
Orchestrate the POTA refresh pipeline (Postgres-backed).

This script:
- does NOT touch SQLite
- relies entirely on DATABASE_URL
- runs the pipeline in the correct order
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from subprocess import run, CalledProcessError
from datetime import datetime, timezone


# -------------------------
# Helpers
# -------------------------
def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def ensure_env():
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL not set. Required for Postgres pipeline.")


def run_step(args, cwd: Path):
    log(" ".join(args))
    try:
        run(args, cwd=cwd, check=True)
    except CalledProcessError as e:
        raise SystemExit(f"Step failed: {' '.join(args)}") from e


# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--active-json", required=True, help="Path to ssb_agent rows JSON")
    ap.add_argument("--history-limit", type=int, default=80)
    ap.add_argument("--edges-limit", type=int, default=2000)
    ap.add_argument("--qrz-limit", type=int, default=100)
    ap.add_argument("--qrz-every-min", type=int, default=10)
    ap.add_argument("--freshness-min", type=int, default=10)
    ap.add_argument("--skip-qrz", action="store_true")
    args = ap.parse_args()

    ensure_env()

    PROJECT_DIR = Path(__file__).resolve().parent

    # Resolve script paths
    pota_history_py = PROJECT_DIR / "pota_history.py"
    qrz_py          = PROJECT_DIR / "qrz_enrich_spotters.py"
    edges_py        = PROJECT_DIR / "build_pota_edges.py"
    status_py       = PROJECT_DIR / "make_park_status.py"

    for p in (pota_history_py, qrz_py, edges_py, status_py):
        if not p.exists():
            raise SystemExit(f"Missing pipeline script: {p}")

    # 1) Fetch POTA history
    log("Fetching POTA spot history")
    run_step(
        [
            sys.executable, str(pota_history_py),
            "--active-json", args.active_json,
            "--limit", str(args.history_limit),
        ],
        cwd=PROJECT_DIR,
    )

    # 2) QRZ enrichment (optional / throttled)
    if not args.skip_qrz:
        log("Enriching spotters via QRZ")
        run_step(
            [
                sys.executable, str(qrz_py),
                "--limit", str(args.qrz_limit),
            ],
            cwd=PROJECT_DIR,
        )

    # 3) Build heard edges
    log("Building heard edges")
    run_step(
        [
            sys.executable, str(edges_py),
            "--limit", str(args.edges_limit),
            "--minutes", str(args.freshness_min),
        ],
        cwd=PROJECT_DIR,
    )

    # 4) Compute park scores / status
    log("Computing park status + scores")
    run_step(
        [
            sys.executable, str(status_py),
            "--window-min", str(args.freshness_min),
        ],
        cwd=PROJECT_DIR,
    )

    log("POTA refresh pipeline complete")


if __name__ == "__main__":
    main()
