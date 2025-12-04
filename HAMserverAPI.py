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

app = FastAPI(
    title="HAM Dashboard API",
    description="Backend service for ham-dashboard.html (POTA / SOTA / DX / CAT / QRZ)",
    version="1.0.0"
)

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
