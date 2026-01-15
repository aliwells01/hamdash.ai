#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
import requests
from datetime import datetime, timezone

import psycopg

QRZ_URL = "https://hamdash-ai.onrender.com/api/qrz/lookup"


def pg_connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set")
    return psycopg.connect(url)


def nowz() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_call(s: str) -> str:
    return (s or "").strip().upper()


# --- Maidenhead grid -> lat/lon (center of subsquare if present) ---
def maidenhead_to_latlon(grid: str):
    """
    Supports 2, 4, 6, 8 char grids (e.g., FM07, FM07ab, FM07ab12).
    Returns (lat, lon) center of the grid cell.
    """
    g = (grid or "").strip()
    if len(g) < 2:
        return None, None

    g = g.upper()
    lon = -180 + (ord(g[0]) - ord("A")) * 20
    lat = -90 + (ord(g[1]) - ord("A")) * 10

    # square (digits)
    if len(g) >= 4 and g[2].isdigit() and g[3].isdigit():
        lon += int(g[2]) * 2
        lat += int(g[3]) * 1
        # center of 2x1 deg square
        lon += 1.0
        lat += 0.5
    else:
        return lat, lon

    # subsquare (letters)
    if len(g) >= 6 and g[4].isalpha() and g[5].isalpha():
        lon += (ord(g[4]) - ord("A")) * (5.0 / 60.0)
        lat += (ord(g[5]) - ord("A")) * (2.5 / 60.0)
        # center of subsquare
        lon += (2.5 / 60.0)
        lat += (1.25 / 60.0)

    # extended square (digits)
    if len(g) >= 8 and g[6].isdigit() and g[7].isdigit():
        lon += int(g[6]) * (5.0 / 600.0)
        lat += int(g[7]) * (2.5 / 600.0)
        # center
        lon += (2.5 / 600.0)
        lat += (1.25 / 600.0)

    return lat, lon


def ensure_table(conn: psycopg.Connection):
    # Safe to keep. Postgres supports CREATE TABLE IF NOT EXISTS.
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS callsign_location (
              callsign       TEXT PRIMARY KEY,
              grid           TEXT,
              city           TEXT,
              state          TEXT,
              country        TEXT,
              lat            DOUBLE PRECISION,
              lon            DOUBLE PRECISION,
              source         TEXT NOT NULL DEFAULT 'qrz',
              updated_at_utc TEXT NOT NULL
            );
            """
        )
    conn.commit()


def fetch_qrz(call: str, timeout_s: int = 60):
    r = requests.get(QRZ_URL, params={"call": call}, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def upsert_loc(conn: psycopg.Connection, call: str, data: dict):
    grid = data.get("grid")
    state = data.get("state")
    country = data.get("country")
    city = data.get("city")  # may not exist
    lat = data.get("lat")
    lon = data.get("lon")

    # If no lat/lon but we have grid, compute from grid
    if (lat is None or lon is None) and grid:
        glat, glon = maidenhead_to_latlon(grid)
        if glat is not None and glon is not None:
            lat, lon = glat, glon

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO callsign_location
              (callsign, grid, city, state, country, lat, lon, source, updated_at_utc)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, 'qrz', %s)
            ON CONFLICT (callsign) DO UPDATE SET
              grid = COALESCE(EXCLUDED.grid, callsign_location.grid),
              city = COALESCE(EXCLUDED.city, callsign_location.city),
              state = COALESCE(EXCLUDED.state, callsign_location.state),
              country = COALESCE(EXCLUDED.country, callsign_location.country),
              lat = COALESCE(EXCLUDED.lat, callsign_location.lat),
              lon = COALESCE(EXCLUDED.lon, callsign_location.lon),
              updated_at_utc = EXCLUDED.updated_at_utc
            """,
            (call, grid, city, state, country, lat, lon, nowz()),
        )
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--sleep", type=float, default=0.8)
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    conn = pg_connect()
    ensure_table(conn)

    # Get uncached spotter calls
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT h.spotter_base
            FROM pota_spot_history h
            LEFT JOIN callsign_location c
              ON c.callsign = h.spotter_base
            WHERE h.spotter_base IS NOT NULL
              AND h.spotter_base <> ''
              AND c.callsign IS NULL
            LIMIT %s
            """,
            (args.limit,),
        )
        rows = cur.fetchall()

    calls = [normalize_call(r[0]) for r in rows if r and r[0]]
    print(f"[info] enriching {len(calls)} calls (limit={args.limit}) using hosted QRZ proxy")

    ok = 0
    for i, call in enumerate(calls, start=1):
        try:
            data = fetch_qrz(call, timeout_s=args.timeout)
            upsert_loc(conn, call, data)
            ok += 1
            grid = data.get("grid")
            st = data.get("state")
            print(f"[{i:03d}] {call}: ok grid={grid} state={st}")
        except requests.HTTPError as e:
            try:
                body = e.response.text
            except Exception:
                body = ""
            print(f"[{i:03d}] {call}: HTTP error {e} {body}")
        except Exception as e:
            print(f"[{i:03d}] {call}: error {e}")

        time.sleep(max(0.0, args.sleep))

    print(f"[done] cached {ok}/{len(calls)}")
    conn.close()


if __name__ == "__main__":
    main()
