#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import math
from datetime import datetime, timezone, timedelta

from db_paths import spots_db_path

def nowz():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def cutoff_iso(minutes: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

def percentile(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * p
    f = int(math.floor(k))
    c = int(math.ceil(k))
    if f == c:
        return float(v[f])
    return float(v[f] + (v[c] - v[f]) * (k - f))

def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))

def score_activation(edges, uniq_spotters, uniq_states, med_km, p75_km, recency_min):
    # recency: strong penalty if getting stale
    if recency_min is None:
        rec = 0.0
    else:
        rec = math.exp(-recency_min / 12.0)

    act = (
        18.0 * math.log1p(edges) +
        14.0 * math.log1p(uniq_spotters) +
        10.0 * math.log1p(uniq_states)
    )

    dist = 0.0
    if med_km is not None:
        if med_km < 200:
            dist -= 10
        elif med_km < 600:
            dist += 0
        elif med_km < 1500:
            dist += 8
        elif med_km < 2500:
            dist += 14
        else:
            dist += 18

    s = (act + dist) * rec
    s = clamp(s, 0.0, 100.0)

    if s < 15:
        label = "Cold"
    elif s < 45:
        label = "Active"
    else:
        label = "Hot"
    return s, label

def iso_to_dt(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="override path to spots.sqlite (optional)")
    ap.add_argument("--window-min", type=int, default=10)
    ap.add_argument("--min-edges", type=int, default=2, help="ignore activations with fewer edges than this")
    args = ap.parse_args()
    db_path = args.db if args.db else spots_db_path()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON;")

    cut = cutoff_iso(args.window_min)
    updated = nowz()
    now_dt = datetime.now(timezone.utc)

    # Overwrite snapshots
    conn.execute("DELETE FROM pota_park_status_now;")
    conn.execute("DELETE FROM pota_park_status_bandmode_now;")

    # --- Park-level aggregation ---
    park_rows = conn.execute(
        """
        SELECT
          e.activator_call,
          e.park_ref,
          MAX(e.spot_time_utc) AS last_edge_time,
          COUNT(*) AS edges,
          COUNT(DISTINCT e.spotter_call) AS uniq_spotters,
          COUNT(DISTINCT c.state) AS uniq_states
        FROM pota_heard_edges e
        LEFT JOIN callsign_location c ON c.callsign = e.spotter_call
        WHERE e.spot_time_utc >= ?
        GROUP BY e.activator_call, e.park_ref
        HAVING COUNT(*) >= ?
        """,
        (cut, args.min_edges),
    ).fetchall()

    dist_rows = conn.execute(
        """
        SELECT activator_call, park_ref, distance_km
        FROM pota_heard_edges
        WHERE spot_time_utc >= ?
          AND distance_km IS NOT NULL
        """,
        (cut,),
    ).fetchall()

    dist_map = {}
    for a, p, km in dist_rows:
        dist_map.setdefault((a, p), []).append(float(km))

    for activator, park, last_edge_time, edges, uniq_spotters, uniq_states in park_rows:
        # Most recent freq/band/mode for this activation
        last_sig = conn.execute(
            """
            SELECT frequency_hz, band, mode
            FROM pota_heard_edges
            WHERE activator_call=? AND park_ref=?
              AND frequency_hz IS NOT NULL
            ORDER BY spot_time_utc DESC
            LIMIT 1
            """,
            (activator, park),
        ).fetchone()

        last_freq_hz = last_sig[0] if last_sig else None
        last_band = last_sig[1] if last_sig else None
        last_mode = last_sig[2] if last_sig else None

        vals = dist_map.get((activator, park), [])
        med = percentile(vals, 0.5)
        p75 = percentile(vals, 0.75)

        last_heard = conn.execute(
            "SELECT last_heard_utc FROM pota_activation_summary WHERE activator_call=? AND park_ref=?",
            (activator, park),
        ).fetchone()

        last_iso = (last_heard[0] if last_heard and last_heard[0] else last_edge_time)
        last_dt = iso_to_dt(last_iso)
        recency_min = (now_dt - last_dt).total_seconds() / 60.0 if last_dt else None

        score, label = score_activation(int(edges), int(uniq_spotters), int(uniq_states), med, p75, recency_min)

        conn.execute(
            """
            INSERT INTO pota_park_status_now
              (activator_call, park_ref, last_heard_utc, window_minutes,
               edges, unique_spotters, unique_states,
               median_km, p75_km,
               score, status, updated_at_utc,
               last_freq_hz, last_band, last_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activator, park, last_iso, args.window_min,
                int(edges), int(uniq_spotters), int(uniq_states),
                med, p75,
                float(score), label, updated,
                last_freq_hz, last_band, last_mode
            ),
        )

    # --- Park+Band+Mode aggregation ---
    bm_rows = conn.execute(
        """
        SELECT
          e.activator_call,
          e.park_ref,
          e.band,
          e.mode,
          MAX(e.spot_time_utc) AS last_edge_time,
          COUNT(*) AS edges,
          COUNT(DISTINCT e.spotter_call) AS uniq_spotters,
          COUNT(DISTINCT c.state) AS uniq_states
        FROM pota_heard_edges e
        LEFT JOIN callsign_location c ON c.callsign = e.spotter_call
        WHERE e.spot_time_utc >= ?
          AND e.band IS NOT NULL
          AND e.mode IS NOT NULL
        GROUP BY e.activator_call, e.park_ref, e.band, e.mode
        HAVING COUNT(*) >= ?
        """,
        (cut, max(1, args.min_edges)),
    ).fetchall()

    bm_dist_rows = conn.execute(
        """
        SELECT activator_call, park_ref, band, mode, distance_km
        FROM pota_heard_edges
        WHERE spot_time_utc >= ?
          AND distance_km IS NOT NULL
          AND band IS NOT NULL AND mode IS NOT NULL
        """,
        (cut,),
    ).fetchall()

    bm_dist_map = {}
    for a, p, b, m, km in bm_dist_rows:
        bm_dist_map.setdefault((a, p, b, m), []).append(float(km))

    for activator, park, band, mode, last_edge_time, edges, uniq_spotters, uniq_states in bm_rows:
        # Most recent frequency for this activation+band+mode
        last_freq = conn.execute(
            """
            SELECT frequency_hz
            FROM pota_heard_edges
            WHERE activator_call=? AND park_ref=? AND band=? AND mode=?
              AND frequency_hz IS NOT NULL
            ORDER BY spot_time_utc DESC
            LIMIT 1
            """,
            (activator, park, band, mode),
        ).fetchone()

        last_freq_hz = last_freq[0] if last_freq else None

        vals = bm_dist_map.get((activator, park, band, mode), [])
        med = percentile(vals, 0.5)
        p75 = percentile(vals, 0.75)

        last_dt = iso_to_dt(last_edge_time)
        recency_min = (now_dt - last_dt).total_seconds() / 60.0 if last_dt else None

        score, label = score_activation(int(edges), int(uniq_spotters), int(uniq_states), med, p75, recency_min)

        conn.execute(
            """
            INSERT INTO pota_park_status_bandmode_now
              (activator_call, park_ref, band, mode, last_heard_utc, window_minutes,
               edges, unique_spotters, unique_states,
               median_km, p75_km,
               score, status, updated_at_utc,
               last_freq_hz)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activator, park, band, mode, last_edge_time, args.window_min,
                int(edges), int(uniq_spotters), int(uniq_states),
                med, p75,
                float(score), label, updated,
                last_freq_hz
            ),
        )

    conn.commit()
    conn.close()
    print(f"[ok] wrote park status tables (window={args.window_min}m) updated={updated}")

if __name__ == "__main__":
    main()
