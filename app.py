# app.py — HAM dashboard backend (updated suggest)
# Features:
# - QRZ XML Callbook lookup (namespace-aware) with session caching
# - HamQTH XML lookup (global fallback) with session caching
# - Callook JSON lookup (US-only fallback)
# - QRZ Logbook INSERT via ADIF
# - FCC License View suggestions for callsigns (US, HA/HV preferred; regex fallback)
# - Diagnostics endpoints
#
# Run:
#   pip install fastapi uvicorn httpx python-dotenv
#   export QRZ_XML_USER='...'; export QRZ_XML_PASS='...'; export QRZ_LOGBOOK_KEY='...'
#   export HAMQTH_USER='...';  export HAMQTH_PASS='...'
#   uvicorn app:app --host 0.0.0.0 --port 8000 --reload

import os
import re
from typing import Optional

import asyncio
import xml.etree.ElementTree as ET   # if not already imported
import httpx  
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from xml.etree import ElementTree as ET
from fastapi import Body



# --- Optional .env support ---
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

app = FastAPI()

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8787"],
    allow_methods=["*"],
    allow_headers=["*"],
)
print("=== app.py LOADED ===")

""" # --- CORS (open by default; tighten if you deploy publicly) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
) """

# --- Config: QRZ ---
QRZ_XML_USER = os.getenv("QRZ_XML_USER")
QRZ_XML_PASS = os.getenv("QRZ_XML_PASS")
QRZ_LOGBOOK_KEY="93C8-8CEC-D13F-CA2B"
""" QRZ_LOGBOOK_KEY = os.getenv("QRZ_LOGBOOK_KEY") """
QRZ_API_KEY = os.getenv("QRZ_API_KEY")


XML_BASE = "https://xmldata.qrz.com/xml/current"
QRZ_API  = "https://logbook.qrz.com/api"

# QRZ XML namespace (default xmlns on root)
QRZ_NS   = "http://xmldata.qrz.com"
QRZMAP   = {"qrz": QRZ_NS}

_qrz_session_key: Optional[str] = None


async def qrz_xml_login() -> str:
    """Log into QRZ XML Callbook and cache the session Key (namespace-aware)."""
    global _qrz_session_key
    if _qrz_session_key:
        return _qrz_session_key

    if not QRZ_XML_USER or not QRZ_XML_PASS:
        raise HTTPException(500, "QRZ XML creds not set (QRZ_XML_USER / QRZ_XML_PASS)")

    headers = {"User-Agent": "hamdash/1.0"}
    params  = {"username": QRZ_XML_USER, "password": QRZ_XML_PASS, "agent": "hamdash"}

    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        r = await client.get(XML_BASE + "/", params=params)

    text = r.text.strip()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise HTTPException(502, f"QRZ XML login failed: bad XML (HTTP {r.status_code})")

    err = root.findtext(".//qrz:Error", namespaces=QRZMAP)
    key = root.findtext(".//qrz:Key",   namespaces=QRZMAP)
    if not key:
        raise HTTPException(502, f"QRZ XML login failed: {err or 'no <Key> in response'}")

    _qrz_session_key = key
    return key


async def qrz_lookup_call(call: str) -> Optional[dict]:
    """Lookup a callsign via QRZ XML Callbook. Returns dict or None."""
    try:
        key = await qrz_xml_login()
    except HTTPException:
        return None

    url = f"{XML_BASE}/?s={key};callsign={call}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
        root = ET.fromstring(r.text)
        cs = root.find(".//qrz:Callsign", namespaces=QRZMAP)
        if cs is None:
            return None
        def t(p: str) -> Optional[str]:
            v = cs.findtext(f"qrz:{p}", namespaces=QRZMAP)
            return v.strip() if v else None
        return {
            "call":    t("call"),
            "name":    t("fname"),
            "surname": t("name"),
            "grid":    t("grid"),
            "country": t("country"),
            "state":   t("state"),
            "cqz":     t("cqzone"),
            "itu":     t("ituzone"),
            "source":  "qrz",
        }
    except Exception:
        return None


# --- Config: HamQTH ---
HAMQTH_USER = os.getenv("HAMQTH_USER")
HAMQTH_PASS = os.getenv("HAMQTH_PASS")
HAMQTH_XML  = "https://www.hamqth.com/xml.php"

HQ_NS   = "https://www.hamqth.com"
HQMAP   = {"hq": HQ_NS}

_hq_session_id: Optional[str] = None


async def hamqth_login() -> str:
    """Get/refresh HamQTH session_id."""
    global _hq_session_id
    if _hq_session_id:
        return _hq_session_id
    if not HAMQTH_USER or not HAMQTH_PASS:
        raise HTTPException(500, "HamQTH creds not set (HAMQTH_USER / HAMQTH_PASS)")

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(HAMQTH_XML, params={"u": HAMQTH_USER, "p": HAMQTH_PASS})

    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        raise HTTPException(502, "HamQTH login failed: bad XML")

    err = root.findtext(".//hq:error", namespaces=HQMAP)
    sid = root.findtext(".//hq:session_id", namespaces=HQMAP)
    if not sid:
        raise HTTPException(502, f"HamQTH login failed: {err or 'no session_id'}")

    _hq_session_id = sid
    return sid


async def hamqth_lookup_call(call: str) -> Optional[dict]:
    """Lookup a callsign via HamQTH."""
    global _hq_session_id
    try:
        sid = await hamqth_login()
    except HTTPException:
        return None

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(HAMQTH_XML, params={"id": sid, "callsign": call, "prg": "hamdash"})
    root = ET.fromstring(r.text)

    err = root.findtext(".//hq:error", namespaces=HQMAP)
    if err:
        if "expired" in err.lower():
            _hq_session_id = None
            try:
                sid = await hamqth_login()
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(HAMQTH_XML, params={"id": sid, "callsign": call, "prg": "hamdash"})
                root = ET.fromstring(r.text)
                err = root.findtext(".//hq:error", namespaces=HQMAP)
                if err:
                    return None
            except Exception:
                return None
        else:
            return None

    s = root.find(".//hq:search", namespaces=HQMAP)
    if s is None:
        return None

    def t(p: str) -> Optional[str]:
        v = s.findtext(f"hq:{p}", namespaces=HQMAP)
        return v.strip() if v else None

    return {
        "call":    t("callsign"),
        "name":    t("adr_name") or t("nick"),
        "grid":    t("grid"),
        "country": t("country"),
        "state":   t("us_state"),
        "cqz":     t("cq"),
        "itu":     t("itu"),
        "source":  "hamqth",
    }

# --- Prefix search helpers (QRZ + HamQTH) ---

async def qrz_search_prefix(prefix: str, limit: int = 12) -> list[str]:
    """
    Use QRZ XML 'Search' with wildcard to get callsigns starting with prefix.
    Requires a valid QRZ XML session (qrz_xml_login()).
    Returns a list[str] of callsigns (uppercased).
    """
    calls = []
    try:
        key = await qrz_xml_login()  # uses your existing cached session
        # QRZ XML supports a Search with wildcard callsign=PREFIX*
        params = {"s": key, "callsign": f"{prefix}*", "agent": "hamdash"}
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(XML_BASE + "/", params=params)
        # Parse XML and collect <Search>/<Callsign>/<call> elements
        root = ET.fromstring(r.text)
        # Namespace-aware search for any <qrz:Search> descendants that contain <qrz:call>
        for cs in root.findall(".//qrz:Search//qrz:Callsign", namespaces=QRZMAP):
            val = cs.findtext("qrz:call", namespaces=QRZMAP)
            if val:
                up = val.strip().upper()
                if up.startswith(prefix.upper()):
                    calls.append(up)
            if len(calls) >= limit:
                break
    except Exception:
        # QRZ search not available / creds not set / XML parse fail -> return empty
        pass
    # dedupe, preserve order
    out, seen = [], set()
    for c in calls:
        if c not in seen:
            seen.add(c)
            out.append(c)
            if len(out) >= limit:
                break
    return out


async def hamqth_search_prefix(prefix: str, limit: int = 12) -> list[str]:
    """
    Use HamQTH XML search with wildcard callsign=PREFIX*.
    Requires hamqth_login() to get a session id.
    Returns a list[str] of callsigns (uppercased).
    """
    calls = []
    try:
        sid = await hamqth_login()  # uses your existing cached session
        params = {"id": sid, "callsign": f"{prefix}*", "prg": "hamdash"}
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(HAMQTH_XML, params=params)
        root = ET.fromstring(r.text)

        # HamQTH search output may have multiple entries; collect any 'callsign' fields under <search>
        s = root.find(".//hq:search", namespaces=HQMAP)
        if s is not None:
            # common patterns seen: <search><callsign>...</callsign> ... or <search><item><callsign>...</callsign></item>...
            # collect generously:
            for tag in s.findall(".//hq:callsign", namespaces=HQMAP):
                val = (tag.text or "").strip().upper()
                if val.startswith(prefix.upper()):
                    calls.append(val)
                if len(calls) >= limit:
                    break
    except Exception:
        pass

    # dedupe, preserve order
    out, seen = [], set()
    for c in calls:
        if c not in seen:
            seen.add(c)
            out.append(c)
            if len(out) >= limit:
                break
    return out






# --- Callook (US-only) ---
def is_us_call(call: str) -> bool:
    return bool(re.match(r'^(?:[KWN][A-Z]?\d[A-Z]{1,3}|A[A-L]\d[A-Z]{1,3})$', call.upper()))


async def callook_lookup(call: str) -> Optional[dict]:
    if not is_us_call(call):
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"https://callook.info/{call}/json")
        j = r.json()
        if j.get("status") != "VALID":
            return None
        cur = j.get("current", {}) or {}
        return {
            "call": call.upper(),
            "name": cur.get("name"),
            "grid": cur.get("grid"),
            "country": "USA",
            "state": cur.get("state"),
            "source": "callook",
        }
    except Exception:
        return None


# --- Public endpoints ---
@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/qrz/lookup")
async def api_lookup(call: str = Query(..., description="Callsign to look up")):
    call = call.strip().upper()

    # 1) QRZ
    data = await qrz_lookup_call(call)
    if data:
        return data

    # 2) HamQTH (global)
    data = await hamqth_lookup_call(call)
    if data:
        return data

    # 3) Callook (US only)
    data = await callook_lookup(call)
    if data:
        return data

    raise HTTPException(404, "Not found")


@app.post("/api/qrz/insert")
async def qrz_insert(
    call: str = Form(...),
    band: str = Form(...),
    mode: str = Form(...),
    

    freq: str = Form(None),
    rst_sent: str = Form(None),
    rst_rcvd: str = Form(None),
    time_on: str = Form(...),      # UTC HHMM
    qso_date: str = Form(...),     # UTC YYYYMMDD
    station_callsign: str = Form(...),
):

    # --- Normalize and validate mode ---
    if not mode or mode.strip() == "":
        mode = "SSB"   # default
    else:
        mode = mode.strip().upper()

    """Insert a QSO into QRZ Logbook via ADIF."""
    if not QRZ_API_KEY:
        raise HTTPException(500, "Missing QRZ_API_KEY")

    def fld(name: str, val: str) -> str:
        return f"<{name}:{len(val)}>{val}"

    parts = [
        fld("call", call.upper()),
        fld("band", band),
        fld("mode", mode),
        fld("station_callsign", station_callsign.upper()),
        "<qso_date:8>"+qso_date,
        "<time_on:4>"+time_on,
    ]
    if freq:     parts.append(fld("freq", freq))
    if rst_sent: parts.append(fld("rst_sent", rst_sent))
    if rst_rcvd: parts.append(fld("rst_rcvd", rst_rcvd))
    adif = "".join(parts) + "<eor>"

    data = {"KEY": QRZ_API_KEY, "ACTION": "INSERT", "ADIF": adif}
    headers = {"User-Agent": "hamdash/1.0"}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(QRZ_API, data=data, headers=headers)

    if resp.status_code != 200 or "RESULT=OK" not in resp.text.upper():
        raise HTTPException(status_code=502, detail=resp.text)

    return {"ok": True}

POTA_HUNTER_CALL = "W5ALI"
POTA_SYNC_FILE = Path(__file__).parent / "pota_last_sync.txt"

def load_last_sync_ts() -> Optional[str]:
    """Return last sync timestamp (YYYYMMDDHHMMSS), or None."""
    if not POTA_SYNC_FILE.exists():
        return None
    try:
        ts = POTA_SYNC_FILE.read_text().strip()
        return ts if ts else None
    except:
        return None

def save_last_sync_ts(ts: str):
    """Save new last-sync timestamp."""
    try:
        POTA_SYNC_FILE.write_text(ts)
    except:
        pass  # non-fatal


@app.post("/api/pota/sync")
async def pota_sync():
    last_ts = load_last_sync_ts()   # can be None (first run)

    pota_url = f"https://api.pota.app/user/qsos/{POTA_HUNTER_CALL}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(pota_url)
    except Exception as e:
        raise HTTPException(502, f"POTA request failed: {e}")
       
    if r.status_code != 200:
        raise HTTPException(502, f"POTA HTTP {r.status_code}")

    try:
        qsos = r.json()
    except Exception as e:
        raise HTTPException(502, f"POTA JSON parse failed: {e}")
     

    from datetime import datetime

    def parse_ts(ts: str):
        """
        Convert ISO timestamp to:
        - qso_date (YYYYMMDD)
        - time_on (HHMM)
        - compare_ts (YYYYMMDDHHMMSS)
        """
        ts = ts.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(ts)
        except:
            return None, None, None

        return (
            dt.strftime("%Y%m%d"),
            dt.strftime("%H%M"),
            dt.strftime("%Y%m%d%H%M%S")
        )

    # Build list of only NEW QSOs
    new_records = []
    newest_uploaded_ts = last_ts

    for q in qsos:
        
        ts_raw = (
            q.get("qsoDateTime") or
            q.get("timeStamp") or
            q.get("timestamp") or
            q.get("dateTime")
        )
        if not ts_raw:
            continue

        qso_date, time_on, compare_ts = parse_ts(ts_raw)
        if not compare_ts:
            continue

        # Skip if older or equal to last sync
        if last_ts and compare_ts <= last_ts:
            continue

        call = (q.get("activator") or q.get("callsign") or "").strip().upper()
        if not call:
            continue

        freq = str(q.get("frequency") or "").strip()
        mode = (q.get("mode") or "SSB").upper()

        park_ref  = (q.get("reference") or "").strip()
        park_name = (q.get("name") or q.get("parkName") or "").strip()
        loc_desc  = (q.get("locationDesc") or "").strip()

        # Extract state from “US-MD”
        state = loc_desc.split("-")[1] if "-" in loc_desc else ""

        comment = " • ".join(filter(None, [
            f"POTA {park_ref}" if park_ref else "",
            park_name,
            state or loc_desc
        ]))

        new_records.append({
            "call": call,
            "mode": mode,
            "freq": freq,
            "qso_date": qso_date,
            "time_on": time_on,
            "park_ref": park_ref,
            "comment": comment,
            "compare_ts": compare_ts
        })

        # Update most recent timestamp
        if not newest_uploaded_ts or compare_ts > newest_uploaded_ts:
            newest_uploaded_ts = compare_ts

    if not new_records:
        return {"ok": True, "count": 0, "detail": "No new POTA QSOs"}

    # Build ADIF
    def field(key, val):
        return f"<{key}:{len(val)}>{val}"

    adif = ""
    for r in new_records:
        parts = [
            field("call", r["call"]),
            field("mode", r["mode"]),
            field("qso_date", r["qso_date"]),
            field("time_on", r["time_on"]),
            field("station_callsign", r["station_callsign"]),
        ]
        if r["freq"]:
            parts.append(field("freq", r["freq"]))
        if r["park_ref"]:
            parts.append(field("sig", "POTA"))
            parts.append(field("sig_info", r["park_ref"]))
        if r["comment"]:
            parts.append(field("comment", r["comment"]))
        adif += "".join(parts) + "<eor>"

    # Upload to QRZ
    data = {"KEY": QRZ_LOGBOOK_KEY, "ACTION": "INSERT", "ADIF": adif}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(QRZ_API, data=data)

        print("=== QRZ RESPONSE FROM POTA SYNC ===")
        print(resp.status_code, resp.text[:500])
        print("===================================")
                

    if resp.status_code != 200 or "RESULT=OK" not in resp.text.upper():
        raise HTTPException(502, f"QRZ insert FAILED: {resp.text[:300]}")

    # Save new timestamp
    if newest_uploaded_ts:
        save_last_sync_ts(newest_uploaded_ts)

    return {"ok": True, "count": len(new_records)}

@app.get("/api/pota/last_sync")
def pota_last_sync():
    """Return last POTA sync timestamp."""
    ts = load_last_sync_ts()
    return {"last_sync": ts or None}




@app.post("/api/pota/paste_sync")
async def pota_paste_sync(qsos: dict = Body(...)):
    print("qsos POSTed from frontend:", qsos)
    print("=== /api/pota/paste_sync CALLED ===")
    qsos = qsos.get("qsos", [])
    # qsos = qsos.get("qsos", [])  # You already did this!

    from datetime import datetime
    def field(n, v): return f"<{n}:{len(v)}>{v}"
    ok_count = 0
    import asyncio

    for q in qsos:
        # Parse fields as per the frontend
        try:
            dt = q["date_time"].split(" ")
            qso_date = dt[0].replace("-", "")
            time_on = dt[1].replace(":", "")[:4]
        except Exception:
            continue
        call = q.get("activator_call", "").strip().upper()
        mode = (q.get("mode") or "SSB").replace("PHONE (", "").replace(")", "").strip().upper()
        band = q.get("band", "").upper()
        park_info = q.get("park_info", "")
        state = q.get("state", "")
        comment = park_info + (f" • {state}" if state else "")
        import re
        ref_match = re.search(r"(US|CA|DX|..)-\d{4,}", park_info)
        park_ref = ref_match.group(0) if ref_match else ""

        parts = [
            field("call", call),
            field("mode", mode),
            field("qso_date", qso_date),
            field("time_on", time_on),
            field("band", band),
            field("station_callsign", "W5ALI"),
        ]
        if park_ref:
            parts.append(field("sig", "POTA"))
            parts.append(field("sig_info", park_ref))
        if comment:
            parts.append(field("comment", comment))
        adif = "".join(parts) + "<eor>\n"
        print("Uploading QSO:", adif)

        data = {"KEY": QRZ_LOGBOOK_KEY, "ACTION": "INSERT", "ADIF": adif}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(QRZ_API, data=data)
            print("QRZ response:", resp.status_code, resp.text[:300])
            if resp.status_code == 200 and "RESULT=OK" in resp.text.upper():
                ok_count += 1
        await asyncio.sleep(1.2)  # Prevent QRZ rate-limiting

    return {"ok": ok_count > 0, "count": ok_count}



@app.get("/api/suggest")
async def suggest(
    q: str = Query(..., min_length=2, description="Callsign prefix (2+ chars)"),
    limit: int = Query(12, ge=1, le=50),
):
    """
    Suggestions for callsigns using QRZ + HamQTH prefix search.
    Returns: {"items": ["K7ABC","W5AL", ...]}
    """
    q = (q or "").strip().upper()
    if len(q) < 2:
        return {"items": []}

    # Fan-out in parallel for low latency; if one fails, the other can still respond
    t_qrz = asyncio.create_task(qrz_search_prefix(q, limit))
    t_hq  = asyncio.create_task(hamqth_search_prefix(q, limit))
    qrz_items, hq_items = await asyncio.gather(t_qrz, t_hq, return_exceptions=True)

    # Normalize exceptions to empty lists
    if isinstance(qrz_items, Exception): qrz_items = []
    if isinstance(hq_items,  Exception): hq_items  = []

    # Merge + dedupe, keep prefix, trim to limit
    seen = set()
    out  = []
    for src in (qrz_items, hq_items):
        for cs in src or []:
            up = (cs or "").strip().upper()
            if not up.startswith(q): 
                continue
            if up in seen: 
                continue
            seen.add(up); out.append(up)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break

    return {"items": out}



# --- Diagnostics ---
@app.get("/api/qrz/diag")
async def qrz_diag():
    env = {
        "QRZ_XML_USER_set": bool(QRZ_XML_USER),
        "QRZ_XML_PASS_set": bool(QRZ_XML_PASS),
        "QRZ_LOGBOOK_KEY_set": bool(QRZ_LOGBOOK_KEY),
        "HAMQTH_USER_set": bool(HAMQTH_USER),
        "HAMQTH_PASS_set": bool(HAMQTH_PASS),
    }
    try:
        key = await qrz_xml_login()
        return {"env": env, "login": "OK", "key_length": len(key)}
    except HTTPException as e:
        return JSONResponse(status_code=502, content={"env": env, "login": "FAIL", "detail": e.detail})


@app.get("/api/qrz/diag_raw")
async def qrz_diag_raw():
    env = {
        "QRZ_XML_USER_set": bool(QRZ_XML_USER),
        "QRZ_XML_PASS_set": bool(QRZ_XML_PASS),
        "QRZ_LOGBOOK_KEY_set": bool(QRZ_LOGBOOK_KEY),
        "HAMQTH_USER_set": bool(HAMQTH_USER),
        "HAMQTH_PASS_set": bool(HAMQTH_PASS),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(XML_BASE + "/", params={"username": QRZ_XML_USER, "password": QRZ_XML_PASS, "agent": "hamdash"})
        raw = r.text.replace("\\n", " ")[:600]
        key = await qrz_xml_login()
        return {"env": env, "login": "OK", "key_length": len(key), "raw_start": raw}
    except HTTPException as e:
        return JSONResponse(status_code=502, content={"env": env, "login": "FAIL", "detail": e.detail})
