# HAMserverAPI.py
#
# This file exposes your ham dashboard backend as a FastAPI service.
# It imports your existing backend modules (app.py and radio_backend.py)
# and makes their functions accessible as clean API endpoints.
#
# Deploy with:
#   uvicorn HAMserverAPI:app --host 0.0.0.0 --port $PORT
#
# Works on Render, Railway, Fly.io, or any normal server.

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware



# Import your own backend logic
import app               # QRZ lookup, CAT control, etc.
import radio_backend     # SOTA, POTA, WWFF, DX, scoring, merging, etc.

app = FastAPI(
    title="HAM Dashboard API",
    description="Backend service for ham-dashboard.html (POTA / SOTA / DX / CAT / QRZ)",
    version="1.0.0"
)

# Allow frontend use from anywhere (GitHub Pages, localhost, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # You may restrict later
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
# Include all routes from app.py
app.include_router(app.router)

# -----------------------------------------------------------
#     QRZ / CALLSIGN LOOKUPS
# -----------------------------------------------------------

@app.get("/api/qrz/{callsign}")
def qrz_lookup(callsign: str):
    """
    Returns QRZ information for a callsign via your app.py logic.
    """
    try:
        return app.qrz_lookup(callsign)
    except Exception as e:
        return {"error": str(e)}

# -----------------------------------------------------------
#     CAT CONTROL (RIG CONTROL)
# -----------------------------------------------------------

@app.post("/api/rig/tune")
def rig_tune(freq_hz: int):
    """
    Tunes the radio to a requested frequency (Hz).
    """
    try:
        return app.tune_rig(freq_hz)
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/rig/frequency")
def rig_get_freq():
    """
    Returns the current tuned frequency of the rig.
    """
    try:
        return app.get_rig_frequency()
    except Exception as e:
        return {"error": str(e)}

# -----------------------------------------------------------
#     SPOT DATA (POTA / SOTA / WWFF / DX / COMBINED)
# -----------------------------------------------------------

@app.get("/api/spots/all")
def get_all_spots():
    """
    Returns all aggregated spots from radio_backend.py
    """
    try:
        return radio_backend.get_all_spots()
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/spots/pota")
def get_pota():
    try:
        return radio_backend.get_pota_spots()
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/spots/sota")
def get_sota():
    try:
        return radio_backend.get_sota_spots()
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/spots/wwff")
def get_wwff():
    try:
        return radio_backend.get_wwff_spots()
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/spots/dx")
def get_dx():
    try:
        return radio_backend.get_dx_spots()
    except Exception as e:
        return {"error": str(e)}

# -----------------------------------------------------------
#     SCORING / DISTANCE / SIGNAL ANALYSIS
# -----------------------------------------------------------

@app.get("/api/score/{call}")
def score_call(call: str):
    """
    Returns scoring info for a callsign (distance, SNR, mode weight, etc.)
    """
    try:
        return radio_backend.score_call(call)
    except Exception as e:
        return {"error": str(e)}

# -----------------------------------------------------------
#     APP STATUS
# -----------------------------------------------------------

@app.get("/api/status")
def status():
    return {"status": "online", "detail": "HAM Dashboard Backend running"}
