#!/usr/bin/env python3
import csv
import sqlite3
from datetime import datetime, timezone
import argparse

from db_paths import spots_db_path

def nowz():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def pick(row, *keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="override path to spots.sqlite (optional)")
    ap.add_argument("--csv", required=True)
    args = ap.parse_args()

    db_path = args.db if args.db else spots_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    updated_at = nowz()
    n = 0

    with open(args.csv, "r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            raise SystemExit("CSV has no header row.")
        print("[info] CSV headers:", r.fieldnames)

        for row in r:
            # Try common POTA park CSV column names
            park_ref = pick(row, "reference", "park_ref", "park", "ref", "id")
            name     = pick(row, "name", "park_name", "parkName")
            entity   = pick(row, "entity", "country", "dxcc", "qthEntity")
            state    = pick(row, "state", "primary_admin_subdivision", "admin1", "region")
            lat      = pick(row, "latitude", "lat", "park_lat")
            lon      = pick(row, "longitude", "lon", "lng", "park_lon")

            if not park_ref:
                continue
            if lat is None or lon is None:
                # Skip parks without coordinates
                continue

            try:
                lat_f = float(lat)
                lon_f = float(lon)
            except Exception:
                continue

            conn.execute(
                """
                INSERT INTO pota_park_cache
                  (park_ref, park_name, lat, lon, entity, state, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(park_ref) DO UPDATE SET
                  park_name = COALESCE(excluded.park_name, pota_park_cache.park_name),
                  lat = excluded.lat,
                  lon = excluded.lon,
                  entity = COALESCE(excluded.entity, pota_park_cache.entity),
                  state = COALESCE(excluded.state, pota_park_cache.state),
                  updated_at_utc = excluded.updated_at_utc
                """,
                (park_ref, name, lat_f, lon_f, entity, state, updated_at),
            )
            n += 1

    conn.commit()
    conn.close()
    print(f"[done] upserted {n} parks into pota_park_cache")

if __name__ == "__main__":
    main()
