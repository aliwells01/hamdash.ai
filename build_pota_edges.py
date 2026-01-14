#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import math
from datetime import datetime, timezone

from db_paths import spots_db_path

def nowz() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def ensure_edges_table(conn: sqlite3.Connection) -> None:
    # In case table wasn’t created for some reason
    conn.execute("""
    CREATE TABLE IF NOT EXISTS pota_heard_edges (
      edge_id            INTEGER PRIMARY KEY AUTOINCREMENT,
      spot_id            INTEGER NOT NULL,
      park_ref           TEXT NOT NULL,
      activator_call     TEXT NOT NULL,
      spotter_call       TEXT NOT NULL,
      band               TEXT,
      mode               TEXT,
      frequency_hz       INTEGER,
      spot_time_utc      TEXT NOT NULL,
      park_lat           REAL,
      park_lon           REAL,
      spotter_lat        REAL,
      spotter_lon        REAL,
      distance_km        REAL,
      azimuth_deg        REAL,
      propagation_score  REAL,
      propagation_meta   TEXT,
      created_at_utc     TEXT NOT NULL,
      FOREIGN KEY(spot_id) REFERENCES pota_spot_history(spot_id) ON DELETE CASCADE
    );
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_pota_edges_spotid ON pota_heard_edges(spot_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pota_edges_park_time ON pota_heard_edges(park_ref, spot_time_utc);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pota_edges_band_time ON pota_heard_edges(band, spot_time_utc);")
    conn.commit()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="override path to spots.sqlite (optional)")
    ap.add_argument("--limit", type=int, default=2000, help="Max edges to build per run")
    ap.add_argument("--minutes", type=int, default=0, help="Only consider history in the last N minutes (0 = all)")
    args = ap.parse_args()

    db_path = args.db if args.db else spots_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    ensure_edges_table(conn)

    where_time = ""
    params = []
    if args.minutes and args.minutes > 0:
        # ISO string compare works because you store times as "YYYY-MM-DDTHH:MM:SSZ"
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.minutes)
        cutoff_iso = cutoff.replace(microsecond=0).isoformat().replace("+00:00","Z")
        where_time = "AND h.spot_time_utc >= ?"
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
      LIMIT ?
    """

    params.append(args.limit)

    rows = conn.execute(query, tuple(params)).fetchall()
    print(f"[info] building edges for {len(rows)} history rows (limit={args.limit})")

    ins = 0
    created = nowz()
    sql_ins = """
      INSERT OR IGNORE INTO pota_heard_edges
      (spot_id, park_ref, activator_call, spotter_call,
      band, mode, frequency_hz, spot_time_utc,
      park_lat, park_lon, spotter_lat, spotter_lon,
      distance_km, azimuth_deg, propagation_score, propagation_meta,
      created_at_utc)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
    """

    cur = conn.cursor()
    for r in rows:
        (spot_id, park_ref, activator, spotter, band, mode, freq_hz, spot_time,
         park_lat, park_lon, spot_lat, spot_lon) = r

        dist = haversine_km(float(park_lat), float(park_lon), float(spot_lat), float(spot_lon))

        cur.execute(sql_ins, (
            int(spot_id),
            park_ref,
            activator,
            spotter,
            band,
            mode,
            freq_hz,
            spot_time,
            float(park_lat),
            float(park_lon),
            float(spot_lat),
            float(spot_lon),
            float(dist),
            created
        ))
        if cur.rowcount == 1:
            ins += 1

    conn.commit()
    conn.close()
    print(f"[done] inserted {ins} edges into pota_heard_edges")

if __name__ == "__main__":
    from datetime import timedelta  # keep local import minimal
    main()
