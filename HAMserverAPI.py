# HAMserverAPI.py
#
# Your NEW, correct FastAPI backend for Render.
# This file exposes QRZ lookup, POTA paste-sync, spot data, rig control,
# and status endpoints, using your existing app.py and radio_backend.py logic.

from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware

# Import your backend modules
import app as qrz_app            # QRZ, paste_sync, CAT control
import radio_backend             # SQLite spot loader
import sqlite3
import psycopg
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from psycopg.types.json import Jsonb

from fastapi import HTTPException

from db_paths import spots_db_path

from loguru import logger

app = FastAPI(
    title="HAM Dashboard API",
    description="Backend service for ham-dashboard.html (POTA / SOTA / DX / CAT / QRZ)",
    version="1.0.0"
)

def pg_connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg.connect(url)


def get_pota_scores_now():
    conn = sqlite3.connect(spots_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT park_ref, score
        FROM pota_park_status_now
        WHERE score IS NOT NULL
    """)

    rows = cur.fetchall()
    conn.close()

    # return { "K-1234": 12.3, ... }
    return {row["park_ref"]: row["score"] for row in rows}



# Allow GitHub Pages or any frontend to use the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Register the QRZ router
app.include_router(qrz_app.router)

# -----------------------------------------------------------
# STATUS CHECK
# -----------------------------------------------------------
@app.get("/api/status")
async def api_status():
    return {"status": "online", "detail": "HAM Dashboard Backend running"}

# -----------------------------------------------------------
# QRZ LOOKUP
# -----------------------------------------------------------
@app.get("/api/qrz/lookup")
async def api_qrz_lookup(call: str):
    """
    Lookup a callsign using app.py's QRZ XML API logic.
    """
    try:
        return await qrz_app.qrz_lookup_call(call)
    except Exception as e:
        return {"error": f"QRZ lookup failed: {e}"}
# -----------------------------------------------------------
# QRZ insert
# -----------------------------------------------------------
@app.get("/api/qrz/insert")
async def api_qrz_insert():
    """
    insert a callsign using app.py's QRZ XML API logic.
    """
    try:
        return await qrz_app.qrz_insert()
    except Exception as e:
        return {"error": f"Insert lookup failed: {e}"}
# -----------------------------------------------------------
# POTA PASTE SYNC
# -----------------------------------------------------------
@app.post("/api/pota/paste_sync")
async def api_pota_paste_sync(payload: dict = Body(...)):
    """
    Upload pasted QSOs to QRZ Logbook using app.py logic.
    """
    try:
        return await qrz_app.pota_paste_sync(payload)
    except Exception as e:
        return {"error": f"POTA paste sync failed: {e}"}

# -----------------------------------------------------------
# SPOTS (SQLite via radio_backend.load_spots)
# -----------------------------------------------------------
@app.get("/api/spots/all")
async def api_spots_all():
    """
    Get waterfall/spot panel data from SQLite via radio_backend.py.
    """
    try:
        return radio_backend.load_spots()
    except Exception as e:
        return {"error": f"Spot load failed: {e}"}

# -----------------------------------------------------------
# CAT CONTROL (proxy to app.py)
# -----------------------------------------------------------
@app.post("/api/rig/tune")
async def api_rig_tune(freq_hz: int):
    try:
        return await qrz_app.tune_rig(freq_hz)
    except Exception as e:
        return {"error": f"Rig tune failed: {e}"}

@app.get("/api/rig/frequency")
async def api_rig_freq():
    try:
        return await qrz_app.get_rig_frequency()
    except Exception as e:
        return {"error": f"Rig frequency failed: {e}"}

# -----------------------------------------------------------
# GEt Pota Scores
# -----------------------------------------------------------

@app.get("/api/pota/park_status_now")
def api_pota_park_status_now():
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT park_ref, score
                    FROM pota_park_status_now
                    WHERE score IS NOT NULL
                """)
                rows = cur.fetchall()

        return {str(park): float(score) for park, score in rows}

    except Exception as e:
        print("[api_pota_park_status_now] ERROR:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))


from fastapi import Request
from fastapi.responses import JSONResponse
import json

@app.post("/api/pota/active_snapshot")
async def set_active_snapshot(req: Request):
    data = await req.json()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

    # Store as compact JSON string
    payload = json.dumps(data, separators=(",", ":"))

    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
              INSERT INTO pota_active_snapshot (id, updated_at_utc, payload_json)
              VALUES (1, %s, %s)
              ON CONFLICT (id) DO UPDATE SET
                updated_at_utc = EXCLUDED.updated_at_utc,
                payload_json = EXCLUDED.payload_json
            """, (now, payload))
        conn.commit()

    return {"ok": True, "updated_at_utc": now}


from fastapi.responses import JSONResponse
import json

@app.get("/api/pota/active_snapshot")
def get_active_snapshot():
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT updated_at_utc, payload_json FROM pota_active_snapshot WHERE id=1")
                row = cur.fetchone()

        if not row:
            return JSONResponse({"ok": False, "error": "no snapshot yet"}, status_code=404)

        updated_at, payload = row

        # Debug info (safe)
        payload_type = str(type(payload))

        # Normalize payload into a Python object
        if payload is None:
            payload_obj = None
        elif isinstance(payload, (dict, list)):
            payload_obj = payload
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            payload_obj = json.loads(bytes(payload).decode("utf-8", errors="replace"))
        elif isinstance(payload, str):
            payload_obj = json.loads(payload)
        else:
            # Last resort: try json.loads on stringified payload
            payload_obj = json.loads(str(payload))

        return {"ok": True, "updated_at_utc": updated_at, "payload": payload_obj, "payload_type": payload_type}

    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": str(e), "error_type": str(type(e))},
            status_code=500
        )


# -----------------------------------------------------------
# Real Band Status 
# -----------------------------------------------------------

from fastapi import HTTPException
from loguru import logger

@app.get("/api/prop/status")
def prop_status():
    sql = """
    SELECT band, score, status, edges, spotters, parks,
           median_km, p75_km, window_minutes, updated_at_utc
    FROM prop_status_band
    ORDER BY
      CASE band
        WHEN '160m' THEN 1 WHEN '80m' THEN 2 WHEN '60m' THEN 3 WHEN '40m' THEN 4
        WHEN '30m' THEN 5 WHEN '20m' THEN 6 WHEN '17m' THEN 7 WHEN '15m' THEN 8
        WHEN '12m' THEN 9 WHEN '10m' THEN 10 WHEN '6m' THEN 11
        ELSE 99
      END;
    """
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.exception("prop_status failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


# -----------------------------------------------------------
# Solar indicies 
# -----------------------------------------------------------


@app.get("/api/prop/solar")
def solar_now():
    sql = """
    SELECT ts_utc, sfi, a_index, k_index, source
    FROM solar_indices
    ORDER BY ts_utc::timestamptz DESC
    LIMIT 1;
    """
    with pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            if not row:
                return {}
            cols = [d.name for d in cur.description]
            return dict(zip(cols, row))

# -----------------------------------------------------------
# Endpoint for JSON upload
# -----------------------------------------------------------
def _parse_ts_epoch(x: Any) -> float:
    if x is None:
        return time.time()
    if isinstance(x, (int, float)):
        v = float(x)
        if v > 1e12:  # ms
            v /= 1000.0
        return v
    if isinstance(x, str):
        s = x.strip()
        try:
            v = float(s)
            if v > 1e12:
                v /= 1000.0
            return v
        except Exception:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return time.time()
    return time.time()


def _spot_to_row(spot: Dict[str, Any]) -> Optional[tuple]:
    callsign = (
        spot.get("callsign") or spot.get("call") or spot.get("tx_call") or spot.get("rx_call") or ""
    ).strip().upper()
    if not callsign:
        return None

    freq_hz = spot.get("freq_hz") or spot.get("frequency_hz") or spot.get("freq")
    if freq_hz is None:
        mhz = spot.get("freq_mhz")
        if mhz is not None:
            try:
                freq_hz = int(round(float(mhz) * 1_000_000))
            except Exception:
                return None

    try:
        freq_hz = int(freq_hz)
    except Exception:
        return None

    mode = (spot.get("mode") or spot.get("mode_raw") or "").strip()
    program = (spot.get("program") or spot.get("comment") or spot.get("source") or "").strip()
    source = (spot.get("src") or spot.get("source") or spot.get("origin") or "").strip()

    snr = spot.get("snr")
    if snr is None:
        snr = spot.get("db") or spot.get("sig") or 0
    try:
        snr = float(snr)
    except Exception:
        snr = 0.0

    ts_epoch = _parse_ts_epoch(spot.get("ts_epoch") or spot.get("ts") or spot.get("time") or spot.get("timestamp"))
    return (ts_epoch, callsign, freq_hz, mode, program, snr, source, Jsonb(spot))



@app.get("/api/debug/spots_live_count")
def spots_live_count():
    try:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM spots_live;")
                n = cur.fetchone()[0]
        return {"ok": True, "count": int(n)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/spots_live/bulk")
async def api_spots_live_bulk(payload: Any = Body(...)):
    try:
        if isinstance(payload, list):
            spots = payload
        elif isinstance(payload, dict):
            spots = payload.get("spots") or payload.get("rows") or []
        else:
            spots = []

        if not isinstance(spots, list) or not spots:
            return {"ok": True, "received": 0, "attempted": 0}

        rows: List[tuple] = []
        skipped = 0
        for s in spots:
            if not isinstance(s, dict):
                skipped += 1
                continue
            r = _spot_to_row(s)
            if r is None:
                skipped += 1
                continue
            rows.append(r)

        if not rows:
            return {"ok": True, "received": len(spots), "attempted": 0, "skipped": skipped}

        sql = """
          INSERT INTO spots_live
            (ts_epoch, callsign, freq_hz, mode, program, snr, source, raw)
          VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s)
          ON CONFLICT (callsign, freq_hz, mode, program, ts_epoch) DO NOTHING
        """

        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
            conn.commit()

        return {"ok": True, "received": len(spots), "attempted": len(rows), "skipped": skipped}

    except Exception as e:
        print("[api_spots_live_bulk] ERROR:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))

