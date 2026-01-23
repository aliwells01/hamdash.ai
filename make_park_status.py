#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import os
from datetime import datetime, timezone

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
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# -------------------------
# Scoring helpers (unchanged logic)
# -------------------------
def norm01(x, lo, hi):
    if x is None:
        return 0.0
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def compute_score(edges, unique_spotters, max_dist_km):
    # This should match your previous behavior
    s_edges = norm01(edges, 1, 15)
    s_unique = norm01(unique_spotters, 1, 12)
    s_dist = norm01(max_dist_km, 50, 6000)
    return round(100.0 * (0.4 * s_edges + 0.35 * s_unique + 0.25 * s_dist), 6)


# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-min", type=int, default=60)
    ap.add_argument("--min-edges", type=int, default=2, help="ignore activations with fewer edges than this")
    args = ap.parse_args()

    window_min = args.window_min
    min_edges = args.min_edges
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_min)
    cutoff_iso = cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    conn = pg_connect()

    # 1) Clear current-status tables
    with conn.cursor() as cur:
        cur.execute("TRUNCATE pota_park_status_now;")
        cur.execute("TRUNCATE pota_active_now;")
        cur.execute("TRUNCATE pota_park_status_bandmode_now;")
    conn.commit()

    # 2) Pull aggregate stats per park from recent edges
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              e.park_ref,
              COUNT(*) AS n_edges,
              COUNT(DISTINCT e.spotter_call) AS unique_spotters,
              MAX(e.distance_km) AS max_dist_km
            FROM pota_heard_edges e
            WHERE e.spot_time_utc >= %s
            GROUP BY e.park_ref
            HAVING COUNT(*) >= %s
            """,
            (cutoff_iso, min_edges),
        )
        rows = cur.fetchall()

    now = nowz()

    # 3) Insert park-level status + score
    with conn.cursor() as cur:
        for park_ref, edges, unique_spotters, max_dist in rows:
            score = compute_score(edges, unique_spotters, max_dist)

            # ---- REQUIRED FIELDS (table has NOT NULL constraints) ----
            activator_call  = ""            # placeholder for now
            last_heard_utc  = now           # best available until refined
            window_minutes  = window_min
            unique_states   = 0
            status          = "ok"

            # ---- METRICS ----
            edges = int(edges)
            unique_spotters = int(unique_spotters)

            # Table has median_km / p75_km, not max_dist_km
            median_km = None
            p75_km    = float(max_dist) if max_dist is not None else None

            last_freq_hz = None
            last_band    = None
            last_mode    = None

            cur.execute(
                """
                INSERT INTO pota_park_status_now
                (activator_call, park_ref, last_heard_utc, window_minutes,
                edges, unique_spotters, unique_states,
                median_km, p75_km,
                score, status, updated_at_utc,
                last_freq_hz, last_band, last_mode)
                VALUES
                (%s,%s,%s,%s,
                %s,%s,%s,
                %s,%s,
                %s,%s,%s,
                %s,%s,%s)
                ON CONFLICT (park_ref) DO UPDATE SET
                activator_call  = EXCLUDED.activator_call,
                last_heard_utc  = EXCLUDED.last_heard_utc,
                window_minutes  = EXCLUDED.window_minutes,
                edges           = EXCLUDED.edges,
                unique_spotters = EXCLUDED.unique_spotters,
                unique_states   = EXCLUDED.unique_states,
                median_km       = EXCLUDED.median_km,
                p75_km          = EXCLUDED.p75_km,
                score           = EXCLUDED.score,
                status          = EXCLUDED.status,
                updated_at_utc  = EXCLUDED.updated_at_utc,
                last_freq_hz    = EXCLUDED.last_freq_hz,
                last_band       = EXCLUDED.last_band,
                last_mode       = EXCLUDED.last_mode
                """,
                (
                    activator_call, park_ref, last_heard_utc, window_minutes,
                    edges, unique_spotters, unique_states,
                    median_km, p75_km,
                    float(score), status, now,
                    last_freq_hz, last_band, last_mode,
                ),
            )


    # 4) Build active-now table (activator + park)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT
              h.activator_call,
              h.park_ref,
              MAX(h.spot_time_utc) AS last_heard_utc,
              COUNT(*) AS history_count,
              COUNT(DISTINCT h.spotter_call) AS unique_spotters
            FROM pota_spot_history h
            WHERE h.spot_time_utc >= %s
            GROUP BY h.activator_call, h.park_ref
            """,
            (cutoff_iso,),
        )
        rows = cur.fetchall()

        rank = 1
        for activator, park_ref, last_heard, hist_n, uniq in rows:
            cur.execute(
                """
                INSERT INTO pota_active_now
                  (rank, activator_call, park_ref, last_heard_utc,
                   history_count, unique_spotters, score, updated_at_utc)
                SELECT
                  %s, %s, %s, %s, %s, %s,
                  ps.score, %s
                FROM pota_park_status_now ps
                WHERE ps.park_ref = %s
                """,
                (rank, activator, park_ref, last_heard, hist_n, uniq, now, park_ref),
            )
            rank += 1

    # 5) Band/mode breakout
    # ---- Band/Mode status rows (Postgres schema-aligned) ----
    cutoff_iso = cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
            h.activator_call,
            h.park_ref,
            h.band,
            h.mode,
            MAX(h.spot_time_utc)                                  AS last_heard_utc,
            %s::bigint                                            AS window_minutes,
            COUNT(*)::bigint                                      AS edges,
            COUNT(DISTINCT h.spotter_call)::bigint                 AS unique_spotters,
            COUNT(DISTINCT c.state)::bigint                        AS unique_states,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY h.distance_km) AS median_km,
            percentile_cont(0.75) WITHIN GROUP (ORDER BY h.distance_km) AS p75_km,
            MAX(h.frequency_hz)::bigint                            AS last_freq_hz
            FROM pota_heard_edges h
            LEFT JOIN callsign_location c
            ON c.callsign = COALESCE(h.spotter_call, '')
            WHERE h.spot_time_utc >= %s
            GROUP BY h.activator_call, h.park_ref, h.band, h.mode
            ORDER BY edges DESC
            """,
            (int(window_min), cutoff_iso),
        )
        bandmode_rows = cur.fetchall()


    with conn.cursor() as cur:
        for (activator_call, park_ref, band, mode,
            last_heard_utc, window_minutes, edges, unique_spotters, unique_states,
            median_km, p75_km, last_freq_hz) in bandmode_rows:

            # Your existing scoring function likely expects (edges, unique_spotters, something_km)
            # If it expects max_dist, use p75_km (or median_km) as the distance proxy:
            score = compute_score(int(edges), int(unique_spotters), float(p75_km or 0.0))

            status = "ok"   # keep simple for now; later you can compute "active/stale"
            now_iso = now   # reuse your existing now (already ISO Z string)

            cur.execute(
                """
                INSERT INTO pota_park_status_bandmode_now
                (activator_call, park_ref, band, mode,
                last_heard_utc, window_minutes,
                edges, unique_spotters, unique_states,
                median_km, p75_km,
                score, status, updated_at_utc,
                last_freq_hz)
                VALUES
                (%s,%s,%s,%s,
                %s,%s,
                %s,%s,%s,
                %s,%s,
                %s,%s,%s,
                %s)
                ON CONFLICT (park_ref, band, mode) DO UPDATE SET
                activator_call  = EXCLUDED.activator_call,
                last_heard_utc  = EXCLUDED.last_heard_utc,
                window_minutes  = EXCLUDED.window_minutes,
                edges           = EXCLUDED.edges,
                unique_spotters = EXCLUDED.unique_spotters,
                unique_states   = EXCLUDED.unique_states,
                median_km       = EXCLUDED.median_km,
                p75_km          = EXCLUDED.p75_km,
                score           = EXCLUDED.score,
                status          = EXCLUDED.status,
                updated_at_utc  = EXCLUDED.updated_at_utc,
                last_freq_hz    = EXCLUDED.last_freq_hz
                """,
                (
                    activator_call, park_ref, band, mode,
                    last_heard_utc, int(window_minutes),
                    int(edges), int(unique_spotters), int(unique_states),
                    median_km, p75_km,
                    float(score), status, now_iso,
                    last_freq_hz,
                ),
            )

        conn.commit()

    conn.close()
    print(f"[done] updated pota park status (window={window_min} min)")


if __name__ == "__main__":
    from datetime import timedelta
    main()
