#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone, timedelta
import math

from db_paths import spots_db_path

def nowz() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def cutoff_iso(minutes: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.replace(microsecond=0).isoformat().replace("+00:00","Z")

def percentile(values, p: float):
    if not values:
        return None
    v = sorted(values)
    k = (len(v)-1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(v[int(k)])
    return float(v[f] + (v[c]-v[f]) * (k-f))

def median(values):
    return percentile(values, 0.5)

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def score_band(edges: int, spotters: int, parks: int, med_km: float | None):
    """
    Score 0..100 based on REAL hearing activity.
    - More edges/spotters/parks increases score.
    - Distance spread matters: very short distances only can mean local-only; mid-range suggests true opens.
      (This is a heuristic; we can tune once you see behavior.)
    """
    # Activity component (log-like growth)
    a = 0.0
    a += 18.0 * math.log1p(edges)      # edges matter most
    a += 12.0 * math.log1p(spotters)
    a += 10.0 * math.log1p(parks)

    # Distance component
    # Encourage mid/long paths: 300–2000 km is a good “skip exists” zone; >2500 km is very strong.
    d = 0.0
    if med_km is not None:
        if med_km < 250:
            d = -10.0
        elif med_km < 600:
            d = 0.0
        elif med_km < 1500:
            d = 8.0
        elif med_km < 2500:
            d = 14.0
        else:
            d = 18.0

    s = clamp(a + d, 0.0, 100.0)

    # Label it
    if s < 15:
        status = "Dead"
    elif s < 35:
        status = "Spotty"
    elif s < 65:
        status = "Open"
    else:
        status = "Hot"

    return s, status

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--window-min", type=int, default=10, help="time window for 'real-time' status")
    ap.add_argument("--top", type=int, default=200, help="how many activations to snapshot in pota_active_now")
    ap.add_argument("--no-active-now", action="store_true", help="skip building pota_active_now")
    args = ap.parse_args()

    sqlite3.connect(spots_db_path())
    conn.execute("PRAGMA foreign_keys = ON;")

    cut = cutoff_iso(args.window_min)
    updated = nowz()

    # ---- Per-band stats from edges ----
    rows = conn.execute(
        """
        SELECT band,
               COUNT(*) AS edges,
               COUNT(DISTINCT spotter_call) AS spotters,
               COUNT(DISTINCT park_ref) AS parks
        FROM pota_heard_edges
        WHERE spot_time_utc >= ?
        GROUP BY band
        """,
        (cut,),
    ).fetchall()

    # We'll also need distances per band for median/p75
    dist_rows = conn.execute(
        """
        SELECT band, distance_km
        FROM pota_heard_edges
        WHERE spot_time_utc >= ?
          AND distance_km IS NOT NULL
        """,
        (cut,),
    ).fetchall()

    dists_by_band = {}
    for b, km in dist_rows:
        if b is None:
            continue
        dists_by_band.setdefault(b, []).append(float(km))

    # overwrite snapshot table
    conn.execute("DELETE FROM prop_status_band;")

    for band, edges, spotters, parks in rows:
        band = band or "?"
        vals = dists_by_band.get(band, [])
        med = median(vals)
        p75 = percentile(vals, 0.75)

        s, label = score_band(int(edges), int(spotters), int(parks), med)

        conn.execute(
            """
            INSERT INTO prop_status_band
              (band, window_minutes, edges, spotters, parks, median_km, p75_km, score, status, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (band, args.window_min, int(edges), int(spotters), int(parks),
             med, p75, float(s), label, updated),
        )

    # ---- Optional: Top-N active activations for UI (fast list) ----
    if not args.no_active_now:
        # We rank by:
        # - recency (last_heard)
        # - unique spotters + history_count (proxy for "hot")
        # - and optionally add "how many edges in window for this activation"
        # We can compute edges_count per (activator,park) from edges table.
        conn.execute("DELETE FROM pota_active_now;")

        act_rows = conn.execute(
            """
            WITH edges_window AS (
              SELECT activator_call, park_ref, COUNT(*) AS ewin
              FROM pota_heard_edges
              WHERE spot_time_utc >= ?
              GROUP BY activator_call, park_ref
            )
            SELECT s.activator_call,
                   s.park_ref,
                   s.last_heard_utc,
                   s.history_count,
                   s.unique_spotters,
                   COALESCE(e.ewin, 0) AS edges_win
            FROM pota_activation_summary s
            LEFT JOIN edges_window e
              ON e.activator_call = s.activator_call AND e.park_ref = s.park_ref
            ORDER BY s.last_heard_utc DESC
            LIMIT ?
            """,
            (cut, args.top),
        ).fetchall()

        # score activation for chasing (simple heuristic for now)
        scored = []
        for activator, park, last_heard, hcount, u_spot, ewin in act_rows:
            
            # Recency boost is already handled by ordering; score focuses on "hotness"
            score = 20.0 * math.log1p(int(hcount)) + 15.0 * math.log1p(int(u_spot)) + 18.0 * math.log1p(int(ewin))
            score = clamp(score, 0.0, 100.0)
            scored.append((activator, park, last_heard, int(hcount), int(u_spot), int(ewin), score))

        # Re-rank by score (hotness) but keep recency implicitly by score definition + data freshness
        scored.sort(key=lambda x: x[-1], reverse=True)

        for idx, (activator, park, last_heard, hcount, u_spot, ewin, score) in enumerate(scored, start=1):
            conn.execute(
                """
                INSERT OR REPLACE INTO pota_active_now
                  (rank, activator_call, park_ref, band, mode, last_heard_utc, history_count, unique_spotters, score, updated_at_utc)
                VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
                """,
                (idx, activator, park, last_heard, hcount, u_spot, float(score), updated),
            )

    conn.commit()
    conn.close()

    print(f"[ok] wrote prop_status_band (window={args.window_min}m) updated={updated}")
    if not args.no_active_now:
        print(f"[ok] wrote pota_active_now top={args.top} updated={updated}")

if __name__ == "__main__":
    main()
