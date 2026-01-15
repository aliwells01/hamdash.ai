#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone, timedelta

import psycopg


# -------------------------
# Postgres helper
# -------------------------
def pg_connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set")
    return psycopg.connect(url)


def nowz() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")


# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-min", type=int, default=15, help="Lookback window in minutes")
    args = ap.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.window_min)
    cutoff_iso = cutoff.replace(microsecond=0).isoformat().replace("+00:00","Z")
    now = nowz()

    conn = pg_connect()

    # 1) Clear current propagation status table
    with conn.cursor() as cur:
        cur.execute("TRUNCATE prop_status_now;")
    conn.commit()

    # 2) Aggregate activity by band + mode
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              h.band,
              h.mode,
              COUNT(*) AS n_spots,
              COUNT(DISTINCT h.spotter_call) AS unique_spotters,
              COUNT(DISTINCT h.activator_call) AS unique_activators,
              MAX(h.spot_time_utc) AS last_heard_utc
            FROM pota_spot_history h
            WHERE h.spot_time_utc >= %s
            GROUP BY h.band, h.mode
            """,
            (cutoff_iso,),
        )
        rows = cur.fetchall()

    # 3) Insert snapshot rows
    with conn.cursor() as cur:
        for band, mode, n_spots, u_spotters, u_activators, last_heard in rows:
            cur.execute(
                """
                INSERT INTO prop_status_now
                  (band, mode, n_spots,
                   unique_spotters, unique_activators,
                   last_heard_utc, updated_at_utc)
                VALUES
                  (%s, %s, %s,
                   %s, %s,
                   %s, %s)
                """,
                (
                    band,
                    mode,
                    n_spots,
                    u_spotters,
                    u_activators,
                    last_heard,
                    now,
                ),
            )

    conn.commit()
    conn.close()
    print(f"[done] updated propagation status (window={args.window_min} min)")


if __name__ == "__main__":
    main()
