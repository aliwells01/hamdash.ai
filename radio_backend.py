# radio_backend.py
import asyncio, json, math, os, socket, sqlite3, struct
import numpy as np
import aiohttp
from aiohttp import web
from datetime import datetime

import os, sys, logging
logging.basicConfig(level=logging.INFO)
logging.info("Python: %s", sys.executable)
logging.info("BACKEND_PORT=%r", os.environ.get("BACKEND_PORT"))

# ---- config ----
RTL_HOST = os.environ.get("RTL_HOST", "127.0.0.1")
RTL_PORT = int(os.environ.get("RTL_PORT", "1234"))
RIG_HOST = os.environ.get("RIG_HOST", "127.0.0.1")
RIG_PORT = int(os.environ.get("RIG_PORT", "4532"))   # 4532 rigctld (G90), or 4533 SDR++ RigCTL server
DB_PATH  = os.environ.get("SPOTS_DB", "/Users/aw/documents/hobbies/ham/radio_intel/data/spots.sqlite")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8787"))  # <— HTTP/WS port

FFT_SIZE = int(os.environ.get("FFT_SIZE", "2048"))
FPS      = int(os.environ.get("FPS", "12"))
SAMPLE_RATE = int(os.environ.get("SAMPLE_RATE", "2048000"))
FFT_WINDOW = np.hanning(FFT_SIZE).astype(np.float32)      # Hann window



RIGCTL_ENABLED = os.environ.get("RIGCTL_ENABLED", "1") == "1"
RIGCTL_HOST    = os.environ.get("RIGCTL_HOST", "127.0.0.1")
RIGCTL_PORT    = int(os.environ.get("RIGCTL_PORT", "4600")) # 4600 rigctld (G90), or 4533 SDR++ RigCTL server


import aiohttp, os
APP_UPSTREAM = os.environ.get("APP_UPSTREAM", "http://127.0.0.1:8000")

tune_queue: asyncio.Queue[int] = asyncio.Queue()


# ---- rtl_tcp command helpers (big-endian) ----
def _cmd(code:int, value:int) -> bytes:
    return struct.pack(">BI", code, value)



CMD_SET_FREQ       = 0x01
CMD_SET_RATE       = 0x02
CMD_SET_GAIN_MODE  = 0x03  # 0: auto, 1: manual
CMD_SET_GAIN       = 0x04  # tenth-dB
CMD_SET_AGC        = 0x08  # 0/1


""" # pack 32-bit command for rtl_tcp (id + big-endian u32)
def _cmd(cmd_id, value):
    return bytes([cmd_id]) + int(value).to_bytes(4, "big", signed=False) """

# app state: center frequency (Hz)
_center_hz = int(os.environ.get("CENTER_HZ", "14230000"))

# ---------- Hamlib/rigctld async client ----------
class RigCtlError(Exception):
    pass

class RigCtlClient:
    """
    Minimal rigctld client. Opens a short TCP connection per command.
    Commands are the same as 'rigctl' (line-oriented).
    """
    def __init__(self, host="127.0.0.1", port=4532, timeout=1.5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._lock = asyncio.Lock()  # serialize commands

    async def _cmd(self, line: str) -> str:
        # run in thread so socket timeout works without blocking the event loop
        def _sync_cmd():
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall((line.strip() + "\n").encode("ascii"))
                # rigctld replies usually end with '\n'; 1KB is plenty for these
                return s.recv(1024).decode("ascii", "ignore").strip()

        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(None, _sync_cmd)

    # --- high-level helpers ---
    async def set_frequency(self, hz: int) -> str:
        return await self._cmd(f"F {int(hz)}")

    async def set_mode(self, mode: str, width: int = 0) -> str:
        """
        mode: 'USB','LSB','CW','CWR','AM','FM','WFM','DIGU','DIGL','RTTY','RTTYR'
        width: pass 0 to let rig decide, or a filter width (Hz) if you want.
        """
        return await self._cmd(f"M {mode} {int(width)}")

    async def set_ptt(self, on: bool) -> str:
        return await self._cmd(f"T {1 if on else 0}")

    async def get_frequency(self) -> int:
        resp = await self._cmd("f")
        try:
            return int(resp)
        except ValueError:
            raise RigCtlError(f"Unexpected freq reply: {resp!r}")

    async def get_mode(self) -> tuple[str, int]:
        # returns e.g. "USB\n2400" or "USB 2400"
        resp = await self._cmd("m")
        parts = resp.replace("\n", " ").split()
        if not parts:
            raise RigCtlError(f"Unexpected mode reply: {resp!r}")
        mode = parts[0]
        width = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return mode, width



# Simple broadcaster to all connected WS clients
class WSHub:
    def __init__(self):
        self.clients = set()
    async def add(self, ws):
        self.clients.add(ws)
    async def remove(self, ws):
        self.clients.discard(ws)
    async def send(self, payload: dict):
        if not self.clients:
            return
        dead = []
        msg = json.dumps(payload)
        for ws in list(self.clients):
            try:
                await ws.send_str(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

hub = WSHub()



# ---- rigctl helpers ----
def rigctl(cmd: str, host=RIG_HOST, port=RIG_PORT, timeout=1.0):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((cmd.strip()+"\n").encode())
        return s.recv(1024).decode(errors="ignore").strip()

# ---- state ----
_center_hz = int(os.environ.get("CENTER_HZ", "14230000"))


async def rtl_tcp_reader(ws):
    global _center_hz
    reader, writer = await asyncio.open_connection(RTL_HOST, RTL_PORT)

    # initial config
    writer.write(_cmd(CMD_SET_RATE, SAMPLE_RATE))
    writer.write(_cmd(CMD_SET_FREQ, _center_hz))
    writer.write(_cmd(CMD_SET_GAIN_MODE, 0))
    writer.write(_cmd(CMD_SET_AGC, 0))
    writer.write(_cmd(CMD_SET_GAIN, int(200)))
    await writer.drain()

    buf_bytes = FFT_SIZE * 4
    window = np.hanning(FFT_SIZE).astype(np.float32)

    try:
        while True:
            # ——— check for pending tune requests ———
            new_hz = None
            while not tune_queue.empty():
                new_hz = await tune_queue.get()
            if new_hz:
                _center_hz = int(new_hz)
                writer.write(_cmd(CMD_SET_FREQ, _center_hz))
                await writer.drain()

            raw = await reader.readexactly(buf_bytes*2)
            iq = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
            iq = (iq - 127.5) / 127.5
            i = iq[0::2]; q = iq[1::2]
            c = i + 1j*q

            if c.size >= FFT_SIZE:
                step = c.size // FFT_SIZE
                c = c[0:step*FFT_SIZE:step]
            else:
                c = np.pad(c, (0, FFT_SIZE - c.size))

            spec = np.fft.fftshift(np.fft.fft(c * window))
            pwr = 20*np.log10(1e-12 + np.abs(spec))

            await ws.send_str(json.dumps({
                "fft": pwr.tolist(),
                "size": FFT_SIZE,
                "sr_hz": SAMPLE_RATE,
                "center_hz": _center_hz
            }))
            await asyncio.sleep(1.0/FPS)
    except asyncio.IncompleteReadError:
        pass
    finally:
        writer.close()
        await writer.wait_closed()



# ---- HTTP API ----
from aiohttp import web
from pathlib import Path
import os, sqlite3, time, traceback

# ---------- CORS ----------
@web.middleware
async def cors_mw(request, handler):
    # Handle preflight early
    if request.method == 'OPTIONS':
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    # CORS headers
    resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
    resp.headers['Vary'] = 'Origin'
    resp.headers['Access-Control-Allow-Credentials'] = 'false'
    resp.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    resp.headers['Access-Control-Max-Age'] = '86400'
    return resp

routes = web.RouteTableDef() 



@routes.get("/pota")
async def serve_pota_html(request):
    return web.FileResponse('./pota/pota-cards-mock.html')  # <-- adjust if in another folder


# ---------- API: spots ----------
@routes.get("/api/spots")
async def api_spots(req):
    DB_PATH = os.environ.get("SPOTS_DB")
    TABLE   = os.environ.get("SPOTS_TABLE", "spots")

    def demo():
        return web.json_response([
            {"freq":14230000,"mode":"USB","callsign":"W4ABC","program":"POTA K-1234","snr":12.3,"age":5,"worked":False},
            {"freq":14074000,"mode":"FT8","callsign":"K1XYZ","program":"SOTA W1/HA-001","snr":7.5,"age":10,"worked":True},
            {"freq": 7055000,"mode":"LSB","callsign":"N0CALL","program":"DX: Spain","snr":18.0,"age":1,"worked":False},
        ])

    try:
        if not DB_PATH or not os.path.exists(DB_PATH):
            print(f"[/api/spots] DB missing or not set: {DB_PATH!r} -> demo")
            return demo()

        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        # Coalesce callsign; freq_mhz→Hz; mode from mode/mode_raw; program from comment/source;
        # SNR from rx/tx; age from ts (epoch or ISO)
        sql = f"""
          SELECT
            COALESCE(NULLIF(call,''), NULLIF(tx_call,''), NULLIF(rx_call,'')) AS callsign,
            CAST(freq_mhz * 1e6 AS INTEGER)                                   AS freq,
            COALESCE(NULLIF(mode,''), NULLIF(mode_raw,''), '')                AS mode,
            COALESCE(NULLIF(comment,''), NULLIF(source,''), '')               AS program,
            COALESCE(p533_rx_snr, p533_tx_snr, 0)                             AS snr,
            ts                                                                AS ts
          FROM "{TABLE}"
          WHERE freq_mhz BETWEEN 0.5 AND 60.0
          ORDER BY ts DESC
          LIMIT 500;
        """
        cur.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
        con.close()

        if not rows:
            print("[/api/spots] Query returned 0 rows -> demo")
            return demo()

        now = time.time()
        out = []
        for r in rows:
            cs = (r.get("callsign") or "").strip().upper()
            if not cs:
                continue
            # age minutes
            age_min = 0
            ts = r.get("ts")
            if ts not in (None, ""):
                try:
                    age_min = max(0, int((now - float(ts)) / 60))
                except:
                    try:
                        import datetime
                        dt = datetime.datetime.fromisoformat(str(ts).replace("Z","").split(".")[0])
                        age_min = max(0, int((datetime.datetime.utcnow() - dt).total_seconds()/60))
                    except:
                        age_min = 0

            out.append({
                "callsign": cs,
                "freq": int(r.get("freq") or 0),
                "mode": r.get("mode") or "",
                "program": r.get("program") or "",
                "snr": float(r.get("snr") or 0),
                "age": age_min,
                "worked": False,
            })

        if not out:
            print("[/api/spots] All rows had empty callsign -> demo")
            print(f"📡 SPOT_DB set to: {SPOT_DB}")
            return demo()

        return web.json_response(out)

    except Exception as e:
        print("[/api/spots] ERROR:", e)
        traceback.print_exc()
        return web.json_response({"error":"spots_query_failed","detail":str(e)}, status=200)


import asyncio, math
from aiohttp import web


@routes.get("/ws/fft")
async def ws_fft(req):
    ws = web.WebSocketResponse()
    await ws.prepare(req)

    task = asyncio.create_task(rtl_tcp_reader(ws))  # ← USE YOUR READER HERE
    try:
        async for _ in ws:  # we don't expect client->server messages
            pass
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await ws.close()
    return ws

    # --- QRZ API shim ---

@routes.get("/api/qrz/ping")
async def qrz_ping(req):
    return web.json_response({"ok": True})







import os
import aiohttp
from aiohttp import web
from datetime import datetime

@routes.post("/api/qrz/insert")
async def qrz_insert(req):
    """Log a QSO to QRZ Logbook via API key."""
    try:
        if req.content_type == "application/json":
            j = await req.json()
        else:
            j = dict((await req.post()).items())
    except:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    # Validate required fields
    if not j.get("callsign"):
        return web.json_response({"ok": False, "error": "missing_callsign"}, status=400)

    # --- Construct ADIF manually ---
    callsign = j.get("callsign", "").upper()
    freq_mhz = str(j.get("freq_mhz", ""))   # cast to string to avoid <freq:0>
    band     = j.get("band", "")
    mode     = j.get("mode", "").upper()
    rst_s    = j.get("rst_s", "59")
    rst_r    = j.get("rst_r", "59")
    station  = os.getenv("QRZ_STATION_CALLSIGN", callsign)

    now = datetime.utcnow()
    qso_date = now.strftime("%Y%m%d")
    time_on  = now.strftime("%H%M")

    adif = (
        f"<call:{len(callsign)}>{callsign}"
        f"<freq:{len(freq_mhz)}>{freq_mhz}"
        f"<mode:{len(mode)}>{mode}"
        f"<band:{len(band)}>{band}"
        f"<rst_sent:{len(rst_s)}>{rst_s}"
        f"<rst_rcvd:{len(rst_r)}>{rst_r}"
        f"<qso_date:8>{qso_date}"
        f"<time_on:4>{time_on}"
        f"<station_callsign:{len(station)}>{station}"
        "<eor>"
    )


    # Send to QRZ
    qrz_key = os.getenv("QRZ_LOGBOOK_KEY")
    if not qrz_key:
        return web.json_response({"ok": False, "error": "missing_qrz_key"}, status=500)
    print("ADIF PAYLOAD:", adif)
    url = "https://logbook.qrz.com/api"
    params = {
        "KEY": qrz_key,
        "ACTION": "INSERT",
        "ADIF": adif
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params) as r:
                body = await r.text()
                if "RESULT=OK" in body and "OK" in body:
                    return web.json_response({"ok": True, "qrz_response": body})
                else:
                    return web.json_response({"ok": False, "error": "qrz_insert_failed", "response": body}, status=400)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)


   



""" async def qrz_insert(req):
    Body: JSON or form — log a QSO to QRZ (or stub).
    try:
        if req.content_type == "application/json":
            j = await req.json()
        else:
            j = dict((await req.post()).items())
    except:
        j = {}
    # Validate minimally
    if not j.get("callsign"):
        return web.json_response({"ok": False, "error":"missing_callsign"}, status=400)

    # TODO: push to QRZ Logbook here; for now just echo back
    return web.json_response({"ok": True, "echo": j}) """



""" @routes.get("/ws/fft")
async def ws_fft(req):
    ws = web.WebSocketResponse()
    await ws.prepare(req)

    size = 1024                # FFT length (UI expects size and fft[])
    t = 0
    try:
        while True:
            # baseline noise at ~-78 dB
            bins = [-78.0] * size

            # two drifting peaks (for a visible “live” look)
            k1 = int(size * (0.25 + 0.02 * math.sin(t / 40)))
            k2 = int(size * (0.62 + 0.02 * math.cos(t / 33)))
            for k in (k1, k2):
                for d in range(-4, 5):
                    i = max(0, min(size - 1, k + d))
                    bins[i] = -28.0 + 6.0 * math.cos(d / 4)
 
            center = int(req.app.get("center_hz") or 14230000)
            await ws.send_json({"size": size, "center_hz": center, "fft": bins})
            await asyncio.sleep(1/12)  # ~12 FPS
            t += 1
    except asyncio.CancelledError:
        pass
    finally:
        await ws.close()
    return ws """



# ---------- API: rig ----------
@routes.get("/api/rig/center_freq")
async def api_center_freq(req):
    center = int(req.app.get("center_hz") or 0)
    return web.json_response({"center_hz": center})



""" @routes.post("/api/rig/tune")
async def api_tune(req):
    j = await req.json()
    hz = int(j.get("freq_hz", 0))
    if hz <= 0:
        return web.json_response({"ok": False, "error": "invalid_freq"}, status=400)
    await tune_queue.put(hz)   # let the rtl loop handle the actual command
    return web.json_response({"ok": True, "center_hz": hz})



    from aiohttp import web """



    # radio_backend.py
from aiohttp import web
import asyncio, json, logging
import websockets 

log = logging.getLogger(__name__)

""" routes = web.RouteTableDef() """

SPECTRUM_WS_URL = "ws://127.0.0.1:8765"   # same host/port as server_spectrum.py

""" @routes.post("/api/rig/tune")
async def api_tune(req: web.Request):
    try:
        j = await req.json()
    except Exception as e:
        log.warning("tune: bad json: %s", e)
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    try:
        hz = int(j.get("freq_hz", 0))
    except Exception:
        hz = 0

    if hz <= 0:
        return web.json_response({"ok": False, "error": "invalid_freq"}, status=400)

    # Bridge to spectrum server over WS
    try:
        async with websockets.connect(SPECTRUM_WS_URL) as ws:
            await ws.send(json.dumps({"cmd": "set_center_hz", "hz": hz}))
            # optional: you could await a small ack; not necessary here
    except Exception as e:
        log.exception("Failed to forward tune to spectrum WS: %s", e)
        return web.json_response({"ok": False, "error": "spectrum_ws_unreachable"}, status=502)

    log.info("Tuned via WS to %d Hz", hz)
    return web.json_response({"ok": True, "center_hz": hz}) """


@routes.post("/api/rig/tune")
async def api_tune(req: web.Request):
    try:
        j = await req.json()
    except Exception as e:
        log.warning("tune: bad json: %s", e)
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    try:
        hz = int(j.get("freq_hz", 0))
    except Exception:
        hz = 0

    if hz <= 0:
        return web.json_response({"ok": False, "error": "invalid_freq"}, status=400)

    # Save locally (optional, used by GET /center_freq)
    req.app["center_hz"] = hz

    # Bridge to spectrum server via websocket
    try:
        async with websockets.connect(SPECTRUM_WS_URL) as ws:
            await ws.send(json.dumps({"cmd": "set_center_hz", "hz": hz}))
    except Exception as e:
        log.exception("Failed to forward tune to spectrum WS: %s", e)
        return web.json_response({"ok": False, "error": "spectrum_ws_unreachable"}, status=502)

    log.info("Tuned via WS to %d Hz", hz)
    return web.json_response({"ok": True, "center_hz": hz})





    


# ---------- API: G90 via rigctld ----------
@routes.post("/api/g90/tune")
async def g90_tune(req):
    if not (RIGCTL_ENABLED and rig):
        return web.json_response({"ok": False, "error": "rigctl_disabled"}, status=503)
    j = await req.json()
    hz = int(j.get("freq_hz", 0))
    if hz <= 0:
        return web.json_response({"ok": False, "error": "invalid_freq"}, status=400)
    try:
        resp = await rig.set_frequency(hz)
        return web.json_response({"ok": True, "freq_hz": hz, "resp": resp})
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=502)

@routes.post("/api/g90/ptt")
async def g90_ptt(req):
    if not (RIGCTL_ENABLED and rig):
        return web.json_response({"ok": False, "error": "rigctl_disabled"}, status=503)
    j = await req.json()
    on = bool(j.get("on", False))
    try:
        resp = await rig.set_ptt(on)
        return web.json_response({"ok": True, "ptt": on, "resp": resp})
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=502)

@routes.post("/api/g90/mode")
async def g90_mode(req):
    if not (RIGCTL_ENABLED and rig):
        return web.json_response({"ok": False, "error": "rigctl_disabled"}, status=503)
    j = await req.json()
    mode = str(j.get("mode", "")).upper()
    width = int(j.get("width", 0))
    if not mode:
        return web.json_response({"ok": False, "error": "missing_mode"}, status=400)
    try:
        resp = await rig.set_mode(mode, width)
        return web.json_response({"ok": True, "mode": mode, "width": width, "resp": resp})
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=502)

@routes.get("/api/g90/status")
async def g90_status(req):
    if not (RIGCTL_ENABLED and rig):
        return web.json_response({"ok": False, "error": "rigctl_disabled"}, status=503)
    try:
        freq = await rig.get_frequency()
        mode, width = await rig.get_mode()
        return web.json_response({"ok": True, "freq_hz": freq, "mode": mode, "width": width})
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=502)


import aiohttp
import os

# Where app.py is listening:
APP_UPSTREAM = os.environ.get("APP_UPSTREAM", "http://127.0.0.1:8000")

async def _proxy_json_get(session, path, params):
    url = APP_UPSTREAM + path
    async with session.get(url, params=params, timeout=10) as r:
        txt = await r.text()
        # Pass through status; force JSON content-type so the UI can .json()
        return web.Response(
            status=r.status,
            text=txt,
            headers={"Content-Type": r.headers.get("Content-Type", "application/json")}
        )

async def _proxy_json_post(session, path, req):
    url = APP_UPSTREAM + path
    # pass through raw body and content-type
    raw = await req.read()
    headers = {"Content-Type": req.headers.get("Content-Type", "application/json")}
    async with session.post(url, data=raw, headers=headers, timeout=15) as r:
        txt = await r.text()
        return web.Response(
            status=r.status,
            text=txt,
            headers={"Content-Type": r.headers.get("Content-Type", "application/json")}
        )

# ---- QRZ / HamQTH proxy (GET) ----
@routes.get("/api/qrz/ping")
async def qrz_ping_proxy(req):
    async with aiohttp.ClientSession() as s:
        return await _proxy_json_get(s, "/qrz/ping", req.query)

@routes.get("/api/qrz/lookup")
async def qrz_lookup_proxy(req):
    async with aiohttp.ClientSession() as s:
        return await _proxy_json_get(s, "/qrz/lookup", req.query)

@routes.get("/api/hamqth/ping")
async def hamqth_ping_proxy(req):
    async with aiohttp.ClientSession() as s:
        return await _proxy_json_get(s, "/hamqth/ping", req.query)

@routes.get("/api/hamqth/lookup")
async def hamqth_lookup_proxy(req):
    async with aiohttp.ClientSession() as s:
        return await _proxy_json_get(s, "/hamqth/lookup", req.query)

# ---- QRZ / HamQTH proxy (POST) ----
@routes.post("/api/qrz/insert")
async def qrz_insert_proxy(req):
    async with aiohttp.ClientSession() as s:
        return await _proxy_json_post(s, "/qrz/insert", req)

@routes.post("/api/hamqth/login")
async def hamqth_login_proxy(req):
    async with aiohttp.ClientSession() as s:
        return await _proxy_json_post(s, "/hamqth/login", req)

@routes.get("/api/suggest")
async def suggest_proxy(req):
    q = (req.rel_url.query.get("q") or "").upper()
    limit = req.rel_url.query.get("limit", "12")

    # If the user didn't type a wildcard, add one for prefix search
    pattern = q if ("*" in q or "?" in q) else (q + "*")

    # Forward to QRZ search endpoint (adjust path/params to your QRZ API)
    params = {
        "pattern": pattern,   # or whatever the QRZ API expects (e.g., 'callsign', 's', etc.)
        "limit": limit,
        # include your QRZ session/auth params here if required
    }

    async with aiohttp.ClientSession() as s:
        async with s.get(APP_UPSTREAM + "/search", params=params, timeout=10) as r:
            body = await r.read()
            ct = r.headers.get("Content-Type", "application/json; charset=utf-8")
            return web.Response(status=r.status, body=body, headers={"Content-Type": ct})


""" 
@routes.get("/api/suggest")
async def suggest_proxy(req):
    params = req.rel_url.query  # pass-through query
    async with aiohttp.ClientSession() as s:
        async with s.get(APP_UPSTREAM + "/suggest", params=params, timeout=10) as r:
            body = await r.read()  # bytes, not text; don't re-encode
            # Pass upstream content-type if provided
            ct = r.headers.get("Content-Type", "application/json; charset=utf-8")
            return web.Response(status=r.status, body=body, headers={"Content-Type": ct}) """
"""       
@routes.get("/api/suggest")
async def suggest_proxy(req):
    # Normalize incoming query
    q = (req.rel_url.query.get('q') or
         req.rel_url.query.get('prefix') or
         req.rel_url.query.get('term') or
         req.rel_url.query.get('call') or
         "").upper()
    limit = req.rel_url.query.get('limit', '12')

    # Send multiple keys so upstream matches at least one
    fwd_params = {
        'q': q,
        'prefix': q,
        'term': q,
        'call': q,
        'limit': limit,
    }

    async with aiohttp.ClientSession() as s:
        async with s.get(APP_UPSTREAM + "/suggest", params=fwd_params, timeout=10) as r:
            body = await r.read()
            ct = r.headers.get("Content-Type", "application/json; charset=utf-8")
            return web.Response(status=r.status, body=body, headers={"Content-Type": ct}) """


# ---------- Build app & routes (order matters!) ----------
app = web.Application(middlewares=[cors_mw])

# CORS preflight for everything
app.router.add_route('OPTIONS', '/{tail:.*}', lambda r: web.Response(status=204))

# Register ALL API routes (this was missing)
app = web.Application()
app.add_routes(routes)

# Serve the dashboard under /ui (after API routes to avoid shadowing)
UI_DIR = Path('/Users/aw/documents/hobbies/ham/ham-ui/')  # contains ham-dashboard.html
app.router.add_static('/ui', path=str(UI_DIR), show_index=True)

# Initialize state used by /api/rig/center_freq
app["center_hz"] = int(os.environ.get("CENTER_HZ", "14230000"))





if __name__ == "__main__":
    port = int(os.environ.get("BACKEND_PORT", "8787"))
    # Optional: print routes for debugging
    for r in app.router.routes():
        try:
            print("ROUTE:", r.method, r.resource.canonical)
        except Exception:
            pass






            
    web.run_app(app, host="127.0.0.1", port=port)
