#!/usr/bin/env python3
from __future__ import annotations

import csv
import argparse
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
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")


def pick(row, *keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Path to POTA parks CSV")
    args = ap.parse_args()

    conn = pg_connect()
    updated_at = nowz()
    inserted = 0

    with open(args.csv, "r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            raise SystemExit("CSV has no header row.")

        print("[info] CSV headers:", r.fieldnames)

        with conn.cursor() as cur:
            for row in r:
                park_ref = pick(row, "reference", "park_ref", "park", "ref", "id")
                name     = pick(row, "name", "park_name", "parkName")
                entity   = pick(row, "entity", "country", "dxcc", "qthEntity")
                state    = pick(row, "state", "primary_admin_subdivision", "admin1", "region")
                lat      = pick(row, "latitude", "lat", "park_lat")
                lon      = pick(row, "longitude", "lon", "lng", "park_lon")

                if not park_ref or lat is None or lon is None:
                    continue

                try:
                    lat_f = float(lat)
                    lon_f = float(lon)
                except Exception:
                    continue

                cur.execute(
                    """
                    INSERT INTO pota_park_cache
                      (park_ref, park_name, lat, lon, entity, state, updated_at_utc)
                    VALUES
                      (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (park_ref) DO UPDATE SET
                      park_name = COALESCE(EXCLUDED.park_name, pota_park_cache.park_name),
                      lat = EXCLUDED.lat,
                      lon = EXCLUDED.lon,
                      entity = COALESCE(EXCLUDED.entity, pota_park_cache.entity),
                      state = COALESCE(EXCLUDED.state, pota_park_cache.state),
                      updated_at_utc = EXCLUDED.updated_at_utc
                    """,
                    (park_ref, name, lat_f, lon_f, entity, state, updated_at),
                )
                if cur.rowcount == 1:
                    inserted += 1

    conn.commit()
    conn.close()
    print(f"[done] upserted {inserted} parks into pota_park_cache")


if __name__ == "__main__":
    main()
