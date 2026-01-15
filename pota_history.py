#!/usr/bin/env python3
"""
Fetch POTA spot history for active activations and store into Postgres.

Populates:
- pota_spot_history (raw rows)
- pota_activation_summary (fast aggregates)

You provide the list of active activations as JSON (from your existing ssb_agent output).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote

import psycopg
import requests
import re

# Strip trailing junk markers used by some spot/cluster sources
RE_TRAIL_HASH = re.compile(r"[-–—]#.*$")          # "-#..." or "–#..."
RE_TRAIL_JUNK = re.compile(r"[-–—](?:QRP|P|M|MM|AM|AAM|QRPP|LH|MOBILE)\b.*$", re.I)

# Common portable suffixes after slash
RE_PORTABLE = re.compile(r"/(?:P|M|MM|AM|AAM|QRP|QRPP)$", re.I)

# Trailing instance counters often like "-1", "-2" etc (only remove when clearly a counter)
RE_TRAIL_COUNTER = re.compile(r"-(\d{1,2})$")

# A conservative ham callsign pattern (not perfect globally, but good for extraction)
RE_CALL_TOKEN = re.compile(r"\b[A-Z0-9]{1,3}\d[A-Z0-9]{1,4}\b", re.I)

POTA_HISTORY_URL = "https://api.pota.app/v1/spots/{activator}/{park}"


def pg_connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set. Point it at your Render Postgres URL.")
    # autocommit False so we can batch commits
    return psycopg.connect(url)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_spot_time(s: str) -> datetime:
    # POTA returns like "2026-01-08T20:18:41" (no Z) - treat as UTC
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def to_hz(freq_str: str | None) -> int | None:
    """POTA history sample shows '14320' (kHz). Convert to Hz int."""
    if not freq_str:
        return None
    try:
        khz = float(freq_str)
        return int(round(khz * 1000))
    except Exception:
        return None


def normalize_callsign(raw_in: str) -> tuple[str, str]:
    """
    Return (raw, base). Keeps raw unchanged except upper/trim, base is cleaned for lookups/joins.
    Conservative: tries hard not to mangle legitimate calls.
    """
    raw = (raw_in or "").strip().upper()
    s = raw

    # 1) Remove "-#..." style markers (CW/cluster artifacts)
    s = RE_TRAIL_HASH.sub("", s).strip()

    # 2) Remove obvious trailing junk segments like "-QRP", "-MOBILE" etc
    s = RE_TRAIL_JUNK.sub("", s).strip()

    # 3) Handle slash forms.
    #    Two common patterns:
    #    - "EA8/AB1CD" (prefix) -> want AB1CD
    #    - "AB1CD/EA8" (suffix) -> want AB1CD
    if "/" in s:
        parts = [p for p in s.split("/") if p]
        for p in reversed(parts):
            if RE_CALL_TOKEN.fullmatch(p):
                s = p
                break
        else:
            s = parts[0]

    # 4) Strip portable suffixes like /P if still present
    s = RE_PORTABLE.sub("", s).strip()

    # 5) Strip trailing "-<n>" counters only if safe
    m = RE_TRAIL_COUNTER.search(s)
    if m:
        candidate = s[:m.start()]
        if RE_CALL_TOKEN.fullmatch(candidate):
            s = candidate

    # 6) Final cleanup
    s = s.strip("- ").strip()

    # 7) If still not a recognizable token, extract one
    if not RE_CALL_TOKEN.fullmatch(s):
        m2 = RE_CALL_TOKEN.search(s)
        if m2:
            s = m2.group(0)

    return raw, s


def upsert_history_rows(conn: psycopg.Connection, rows: list[dict], activator_call: str, park_ref: str) -> int:
    """
    Insert history rows. spot_id is primary key; duplicates are ignored.
    Returns inserted count.
    """
    inserted = 0
    fetched_at = utc_now_iso()

    sql = """
    INSERT INTO pota_spot_history
      (spot_id, spot_time_utc, activator_call, park_ref,
       spotter_call, spotter_base, band, mode, frequency_hz, comments, fetched_at_utc)
    VALUES
      (%s, %s, %s, %s,
       %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (spot_id) DO NOTHING
    """

    with conn.cursor() as cur:
        for r in rows:
            spot_id = r.get("spotId")
            spot_time = r.get("spotTime")
            spotter_raw, spotter_base = normalize_callsign(r.get("spotter"))
            mode = r.get("mode")
            freq_hz = to_hz(r.get("frequency"))
            band = r.get("band")
            comments = r.get("comments")

            if spot_id is None or not spot_time or not spotter_raw:
                continue

            try:
                dt = parse_spot_time(spot_time)
                spot_time_utc = dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            except Exception:
                continue

            cur.execute(
                sql,
                (
                    int(spot_id),
                    spot_time_utc,
                    activator_call,
                    park_ref,
                    spotter_raw,
                    spotter_base,
                    band,
                    mode,
                    freq_hz,
                    comments,
                    fetched_at,
                ),
            )
            # psycopg rowcount == 1 only when inserted (DO NOTHING => 0)
            if cur.rowcount == 1:
                inserted += 1

    conn.commit()
    return inserted


def update_summary(conn: psycopg.Connection, activator_call: str, park_ref: str) -> None:
    """
    Update pota_activation_summary for this activator+park based on pota_spot_history.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              MAX(spot_time_utc) AS last_heard,
              COUNT(*) AS n,
              COUNT(DISTINCT spotter_call) AS u
            FROM pota_spot_history
            WHERE activator_call = %s AND park_ref = %s
            """,
            (activator_call, park_ref),
        )
        row = cur.fetchone()

    if not row or not row[0]:
        return

    last_heard, n, u = row[0], row[1], row[2]
    now = utc_now_iso()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pota_activation_summary
              (activator_call, park_ref, last_heard_utc, history_count, unique_spotters, last_fetched_utc)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (activator_call, park_ref) DO UPDATE SET
              last_heard_utc = EXCLUDED.last_heard_utc,
              history_count = EXCLUDED.history_count,
              unique_spotters = EXCLUDED.unique_spotters,
              last_fetched_utc = EXCLUDED.last_fetched_utc
            """,
            (activator_call, park_ref, last_heard, int(n), int(u), now),
        )

    conn.commit()


def fetch_history(activator_call: str, park_ref: str, timeout: float = 15.0) -> list[dict]:
    activator_enc = quote(activator_call, safe="")
    url = POTA_HISTORY_URL.format(activator=activator_enc, park=park_ref)
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "spots" in data and isinstance(data["spots"], list):
        return data["spots"]
    return []


def load_active_list_from_json(path: str) -> list[tuple[str, str]]:
    """
    Expects JSON list of rows like ssb_agent output.
    Looks for:
      - activator call in 'call'
      - park ref in 'comment'
      - src == "POTA"
    Returns list of (activator_call, park_ref)
    """
    with open(path, "r", encoding="utf-8") as f:
        j = json.load(f)

    if isinstance(j, dict) and "rows" in j:
        j = j["rows"]

    if not isinstance(j, list):
        return []

    pairs: set[tuple[str, str]] = set()
    for r in j:
        if not isinstance(r, dict):
            continue
        if r.get("src") != "POTA":
            continue
        call = r.get("call")
        park = r.get("comment")
        if not call or not park:
            continue
        if isinstance(park, str) and ("-" in park) and len(park) >= 5:
            pairs.add((str(call), str(park)))

    return sorted(pairs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--active-json", required=True, help="Path to JSON rows from ssb_agent (e.g., ssb_picks.rows.json)")
    ap.add_argument("--limit", type=int, default=80, help="Max activations to fetch history for")
    ap.add_argument("--sleep", type=float, default=0.25, help="Sleep between API calls (be polite)")
    args = ap.parse_args()

    activations = load_active_list_from_json(args.active_json)
    if not activations:
        raise SystemExit("No active POTA activations found in active JSON.")

    activations = activations[: max(1, args.limit)]

    print(f"[info] will fetch history for {len(activations)} activations (limit={args.limit})")
    conn = pg_connect()

    total_inserted = 0
    for i, (activator_call, park_ref) in enumerate(activations, start=1):
        try:
            hist = fetch_history(activator_call, park_ref)
            ins = upsert_history_rows(conn, hist, activator_call, park_ref)
            update_summary(conn, activator_call, park_ref)
            total_inserted += ins
            print(f"[{i:03d}/{len(activations)}] {activator_call} {park_ref}: history={len(hist)} inserted={ins}")
        except Exception as e:
            print(f"[warn] {activator_call} {park_ref}: {e}")

        time.sleep(max(0.0, args.sleep))

    conn.close()
    print(f"[done] inserted {total_inserted} new pota_spot_history rows")


if __name__ == "__main__":
    main()
