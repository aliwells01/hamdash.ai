#!/usr/bin/env python3
"""
run_sota_refresh.py

Ingest SOTA rows from ssb_agent's *.rows.json into Postgres spots_live via HAMserverAPI.

Why:
- Your POTA pipeline is park-based and intentionally POTA-only.
- We keep it unchanged.
- This script gives SOTA a parallel "live" feed, preserving program identity via spots_live.source.

How it works:
- Reads ssb_agent rows JSON (list of dicts).
- Filters rows where src == "SOTA".
- Sends them to HAMserverAPI POST /api/spots_live/bulk.
  HAMserverAPI will map:
    callsign <- call
    freq_hz  <- freq_mhz * 1e6 (we send freq_mhz)
    program  <- comment (summit code)
    source   <- src (SOTA)
    ts_epoch <- parsed from "time" (we send time_utc as "time")
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List

import requests


def load_rows(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Accept either a list of rows OR a wrapper dict that contains the rows list.
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        # try common wrapper keys
        for k in ("rows", "spots", "data", "payload", "items"):
            v = data.get(k)
            if isinstance(v, list):
                rows = v
                break
        else:
            # sometimes payload itself wraps another object
            v = data.get("payload")
            if isinstance(v, dict):
                for k in ("rows", "spots", "data", "items"):
                    vv = v.get(k)
                    if isinstance(vv, list):
                        rows = vv
                        break
                else:
                    raise ValueError(
                        f"Expected a JSON list or wrapper containing a list in {path}. "
                        f"Top-level keys={list(data.keys())[:20]}"
                    )
            else:
                raise ValueError(
                    f"Expected a JSON list or wrapper containing a list in {path}. "
                    f"Top-level keys={list(data.keys())[:20]}"
                )
    else:
        raise ValueError(f"Expected JSON list/dict in {path}, got {type(data)}")

    # keep only dict rows
    return [r for r in rows if isinstance(r, dict)]



def sota_payload(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        if (r.get("src") or "").upper() != "SOTA":
            continue

        call = (r.get("call") or "").strip()
        freq_mhz = r.get("freq")
        mode = (r.get("mode") or "").strip()
        summit = (r.get("comment") or "").strip()
        time_utc = r.get("time_utc")  # e.g. "2026-01-29T18:18:37Z"

        if not call or freq_mhz is None:
            continue

        # Build a dict that HAMserverAPI._spot_to_row understands
        # - callsign: "call"
        # - freq: use freq_mhz so API converts to Hz
        # - program: use summit code in "comment" or "program"
        # - source: use "src"
        # - time: API parses ISO timestamps (we pass time_utc through "time")
        payload_row = {
            "call": call,
            "freq_mhz": freq_mhz,
            "mode": mode,
            "comment": summit,   # HAMserverAPI uses comment as program
            "src": "SOTA",       # HAMserverAPI uses src as source
            "time": time_utc,    # parsed into ts_epoch
            # Keep everything else in raw JSON for debugging/UI later
            "score": r.get("score"),
            "band": r.get("band"),
            "dist_mi": r.get("dist_mi"),
            "lat": r.get("lat"),
            "lon": r.get("lon"),
            "link": r.get("link"),
            "geo": r.get("geo"),
        }

        out.append(payload_row)

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--active-json", required=True, help="Path to ssb_agent rows JSON (e.g. ssb_picks.rows.json)")
    ap.add_argument(
        "--api-base",
        default=os.environ.get("HAM_API_BASE", "http://localhost:8000"),
        help="HAMserverAPI base URL (default: env HAM_API_BASE or http://localhost:8000)",
    )
    ap.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout seconds (default 15)")
    args = ap.parse_args()

    rows = load_rows(args.active_json)
    spots = sota_payload(rows)

    print(f"[run_sota_refresh] rows_in={len(rows)} sota_rows={len(spots)} api={args.api_base}")

    if not spots:
        print("[run_sota_refresh] No SOTA rows to ingest. Done.")
        return 0

    url = args.api_base.rstrip("/") + "/api/spots_live/bulk"

    try:
        resp = requests.post(url, json={"spots": spots}, timeout=args.timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[run_sota_refresh] ERROR posting to {url}: {e}", file=sys.stderr)
        return 2

    print(f"[run_sota_refresh] ok={data.get('ok')} received={data.get('received')} attempted={data.get('attempted')} skipped={data.get('skipped')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
