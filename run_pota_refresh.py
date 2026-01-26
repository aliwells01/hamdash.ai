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
# Solar indicies
# -------------------------

import requests
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

HAMQSL_SOLAR_URL = "https://www.hamqsl.com/solarxml.php"

def fetch_and_store_solar_indices(conn):
    """
    Fetch SFI / A / K from HamQSL and store in solar_indices.
    Runs once per workflow execution.
    """
    try:
        r = requests.get(HAMQSL_SOLAR_URL, timeout=10)
        r.raise_for_status()

        root = ET.fromstring(r.text)

        sfi = int(root.findtext(".//solarflux", default="0"))
        a_index = int(root.findtext(".//aindex", default="0"))
        k_index = float(root.findtext(".//kindex", default="0"))

        ts_utc = datetime.now(timezone.utc).isoformat()

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO solar_indices (ts_utc, sfi, a_index, k_index, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (ts_utc) DO NOTHING
                """,
                (ts_utc, sfi, a_index, k_index, "hamqsl"),
            )

        conn.commit()
        logger.info(f"[solar] SFI={sfi} A={a_index} K={k_index}")

    except Exception as e:
        logger.warning(f"[solar] failed to fetch solar indices: {e}")






import os
import psycopg
from loguru import logger

UPSERT_PROP_STATUS_BAND_SQL = """
WITH agg AS (
  SELECT
    band,
    MAX(window_minutes) AS window_minutes,
    SUM(edges)::bigint AS edges,
    SUM(unique_spotters)::bigint AS spotters,
    COUNT(DISTINCT park_ref)::bigint AS parks,

    CASE
      WHEN SUM(edges) > 0 THEN SUM(median_km * edges) / SUM(edges)
      ELSE AVG(median_km)
    END AS median_km,

    CASE
      WHEN SUM(edges) > 0 THEN SUM(p75_km * edges) / SUM(edges)
      ELSE AVG(p75_km)
    END AS p75_km,

    CASE
      WHEN SUM(edges) > 0 THEN SUM(score * edges) / SUM(edges)
      ELSE AVG(score)
    END AS score,

    MAX(updated_at_utc) AS updated_at_utc
  FROM pota_park_status_bandmode_now
  GROUP BY band
),
with_freshness AS (
  SELECT
    a.*,
    (now() AT TIME ZONE 'UTC') - (a.updated_at_utc::timestamptz) AS age_interval
  FROM agg a
),
final AS (
  SELECT
    band,
    window_minutes,
    edges,
    spotters,
    parks,
    median_km,
    p75_km,
    score,
    CASE
      WHEN age_interval > interval '10 minutes' THEN 'STALE'
      ELSE 'FRESH'
    END AS status,
    updated_at_utc
  FROM with_freshness
)

INSERT INTO prop_status_band (
  band, window_minutes, edges, spotters, parks, median_km, p75_km, score, status, updated_at_utc
)
SELECT
  band, window_minutes, edges, spotters, parks, median_km, p75_km, score, status, updated_at_utc
FROM final
ON CONFLICT (band) DO UPDATE SET
  window_minutes = EXCLUDED.window_minutes,
  edges          = EXCLUDED.edges,
  spotters       = EXCLUDED.spotters,
  parks          = EXCLUDED.parks,
  median_km      = EXCLUDED.median_km,
  p75_km         = EXCLUDED.p75_km,
  score          = EXCLUDED.score,
  status         = EXCLUDED.status,
  updated_at_utc = EXCLUDED.updated_at_utc;
"""

def upsert_prop_status_band(db_url: str) -> None:
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(UPSERT_PROP_STATUS_BAND_SQL)
        conn.commit()
    logger.info("prop_status_band upsert complete")




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

        # 5) Compute band-level propagation status
    log("Computing band propagation status (prop_status_band)")
    db_url = os.environ["DATABASE_URL"]
    upsert_prop_status_band(db_url)
    fetch_and_store_solar_indices(db_url)

    log("POTA refresh pipeline complete")


if __name__ == "__main__":
    main()
