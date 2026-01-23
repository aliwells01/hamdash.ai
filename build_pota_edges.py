#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
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
# Geo helpers
# -------------------------
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c


# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=2000, help="Max edges to build per run")
    ap.add_argument("--minutes", type=int, default=0, help="Only consider history in last N minutes (0 = all)")
    args = ap.parse_args()

    conn = pg_connect()

    where_time = ""
    params = []

    if args.minutes and args.minutes > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.minutes)
        cutoff_iso = cutoff.replace(microsecond=0).isoformat().replace("+00:00","Z")
        where_time = "AND h.spot_time_utc >= %s"
        params.append(cutoff_iso)

    # Pull history rows that:
    # - do not already have an edge
    # - have spotter lat/lon
    # - have park lat/lon
    query = f"""
      SELECT
        h.spot_id,
        h.spot_time_utc,
        h.activator_call,
        h.park_ref,
        h.spotter_call,
        h.spotter_base,
        h.band,
        h.mode,
        h.frequency_hz,
        p.lat AS park_lat,
        p.lon AS park_lon,
        c.lat AS spotter_lat,
        c.lon AS spotter_lon
      FROM pota_spot_history h
      JOIN pota_park_cache p
        ON p.park_ref = h.park_ref
      JOIN callsign_location c
        ON c.callsign = COALESCE(h.spotter_base, h.spotter_call)
      LEFT JOIN pota_heard_edges e
        ON e.spot_id = h.spot_id
      WHERE e.spot_id IS NULL
        {where_time}
      ORDER BY h.spot_time_utc DESC
      LIMIT %s
    """

    params.append(args.limit)

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        rows = cur.fetchall()

    print(f"[info] building edges for {len(rows)} history rows (limit={args.limit})")

    created = nowz()
    inserted = 0

    insert_sql = """
      INSERT INTO pota_heard_edges
      (spot_id, park_ref, activator_call, spotter_call,
       band, mode, frequency_hz, spot_time_utc,
       park_lat, park_lon, spotter_lat, spotter_lon,
       distance_km, azimuth_deg, propagation_score,
       propagation_meta, created_at_utc)
      VALUES
      (%s, %s, %s, %s,
       %s, %s, %s, %s,
       %s, %s, %s, %s,
       %s, NULL, NULL,
       NULL, %s)
    """

    with conn.cursor() as cur:
      for (
          spot_id, spot_time, activator, park_ref,
          spotter_call, spotter_base, band, mode, freq_hz,
          park_lat, park_lon, spot_lat, spot_lon
      ) in rows:

        # Skip rows missing any coordinates (prevents float(None) crash)
        if park_lat is None or park_lon is None or spot_lat is None or spot_lon is None:
            continue

        try:
            dist = haversine_km(
                float(park_lat), float(park_lon),
                float(spot_lat), float(spot_lon)
            )
        except Exception:
            continue

        cur.execute(
            insert_sql,
            (
                int(spot_id),
                park_ref,
                activator,
                spotter_call,
                band,
                mode,
                freq_hz,
                spot_time,
                float(park_lat),
                float(park_lon),
                float(spot_lat),
                float(spot_lon),
                float(dist),
                created,
            ),
        )

        if cur.rowcount == 1:
            inserted += 1

    conn.commit()

    conn.close()

    print(f"[done] inserted {inserted} edges into pota_heard_edges")


if __name__ == "__main__":
    main()
