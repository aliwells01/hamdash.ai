#!/usr/bin/env python3
"""
HF spot aggregator with band+mode filters, DX Summit, optional HamAlert (file buffer), DXWatch scraping,
distance-first scoring, time-decay, and a zoom/pan-friendly waterfall.

Changes in this version:
- Hard reset waterfall on any quick-view / zoom / pan / reset so old marks never linger.
- Score includes freshness multiplier (exponential time-decay; half-life = 45 min).
- Spots older than 6 hours are dropped entirely.
"""

from __future__ import annotations

import html, json, math, os, re, shutil, subprocess, tempfile, time
import re, requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import html as _html
import re, requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os
import requests


# Prefer IPv4 (avoids IPv6 connect stalls on some networks)
import socket
try:
    import requests.packages.urllib3.util.connection as urllib3_cn
    urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass



# for the robust version only:
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Any
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

# ---------------------- QTH / Antenna ----------------------
MY_GRID = "EM73ts"  # Atlanta
MY_POWER_W = 100
DIPOLE_H_40M = 10.0
DIPOLE_H_20M = 12.0

from datetime import datetime, timezone

import os
import requests

POTA_ACTIVE_SNAPSHOT_POST_URL = os.environ.get("POTA_ACTIVE_SNAPSHOT_POST_URL", "").strip()
API_BASE = os.environ.get("API_BASE", "").strip()  # optional legacy support

def post_snapshot(rows):
    """
    Post active POTA rows to the API snapshot endpoint.
    Prefer explicit POTA_ACTIVE_SNAPSHOT_POST_URL; fall back to API_BASE if set.
    Non-fatal by default (matches your style), but logs HTTP errors.
    """
    url = POTA_ACTIVE_SNAPSHOT_POST_URL
    if not url and API_BASE:
        url = f"{API_BASE}/api/pota/active_snapshot"
    if not url:
        return  # skip if not configured

    try:
        r = requests.post(url, json={"rows": rows}, timeout=15)
        if r.status_code >= 300:
            print("[warn] active_snapshot post failed:", r.status_code, r.text[:300])
        else:
            print("[info] active_snapshot posted:", r.status_code)
    except Exception as e:
        print("[warn] active_snapshot post exception:", e)




def _pick(obj, name):
    # works for both objects and dicts
    if isinstance(obj, dict):
        return obj.get(name, None)
    return getattr(obj, name, None)

def extract_timestamp(obj):
    """
    Return an aware UTC datetime or None from a spot-like object/dict.
    Tries multiple common fields and formats (epoch, ISO8601).
    """
    v = (
        _pick(obj, "ts")
        or _pick(obj, "time_utc")
        or _pick(obj, "timestamp_utc")
        or _pick(obj, "timestamp")
        or _pick(obj, "datetime")
        or _pick(obj, "dt")
        or _pick(obj, "timeStamp")
        or _pick(obj, "time")
    )
    if v is None:
        return None

    # numeric epoch?
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone.utc)

    # string?
    if isinstance(v, str):
        t = v.strip()
        # epoch in string
        if t.isdigit():
            return datetime.fromtimestamp(float(t), tz=timezone.utc)
        # ISO8601
        try:
            if t.endswith("Z"):
                t2 = t[:-1]
                dt = datetime.fromisoformat(t2)
                return dt.replace(tzinfo=timezone.utc if dt.tzinfo is None else dt.tzinfo).astimezone(timezone.utc)
            dt = datetime.fromisoformat(t)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    return None  # give up gracefully


def maidenhead_to_latlon(grid: str) -> Tuple[float, float]:
    g = grid.strip().upper()
    if len(g) < 2: raise ValueError("Invalid grid square")
    lon = (ord(g[0]) - 65) * 20 - 180
    lat = (ord(g[1]) - 65) * 10 - 90
    if len(g) >= 4:
        lon += (ord(g[2]) - 48) * 2
        lat += (ord(g[3]) - 48) * 1
    if len(g) >= 6:
        lon += (ord(g[4]) - 65) * (5/60)
        lat += (ord(g[5]) - 65) * (2.5/60)
    if len(g) >= 8:
        lon += (ord(g[6]) - 48) * (5/600)
        lat += (ord(g[7]) - 48) * (2.5/600)
    res_lon = {2:10,4:1,6:5/60,8:5/600}.get(len(g),10)
    res_lat = {2:5,4:0.5,6:2.5/60,8:2.5/600}.get(len(g),5)
    lon += res_lon/2; lat += res_lat/2
    return (lat, lon)

MY_LAT, MY_LON = maidenhead_to_latlon(MY_GRID)

# ---------------------- Great-circle distance ----------------------
EARTH_R_MI = 3958.7613  # miles
def haversine_miles(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    φ1, φ2 = math.radians(a_lat), math.radians(b_lat)
    dφ = math.radians(b_lat - a_lat)
    dλ = math.radians(b_lon - a_lon)
    s = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return 2 * EARTH_R_MI * math.asin(math.sqrt(max(0.0, min(1.0, s))))

# ---------------------- Bands (meters) ----------------------
BAND_RANGES = [
    (160, 1.8, 2.0),
    (80,  3.5, 4.1),
    (60,  5.0, 5.5),
    (40,  7.0, 7.3),
    (30, 10.1, 10.15),
    (20, 14.0, 14.35),
    (17, 18.068, 18.168),
    (15, 21.0, 21.45),
    (12, 24.89, 24.99),
    (10, 28.0, 29.7),
]
def freq_to_meters(freq_mhz: float) -> Optional[int]:
    f = float(freq_mhz)
    for meters, lo, hi in BAND_RANGES:
        if lo <= f <= hi:
            return meters
    return None

# ---------------------- Frequency normalization (kHz → MHz) ----------------------
def normalize_freq_mhz(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except Exception:
        try: f = float(str(v).replace(",", "").strip())
        except Exception: return None
    if f > 1800.0:  # likely kHz from some feeds
        f = f / 1000.0
    if 1.5 <= f <= 30.0:
        return f
    return None

# ---------------------- Phone sub-bands (US General SSB) ----------------------
PHONE_SUBBANDS = [
    (160, 1.840, 2.000),
    (80,  3.800, 4.000),
    (40,  7.175, 7.300),
    (20, 14.225, 14.350),
    (17, 18.110, 18.168),
    (15, 21.275, 21.450),
    (12, 24.930, 24.990),
    (10, 28.300, 29.700),
]
def in_phone_subband(freq_mhz: float) -> bool:
    b = freq_to_meters(freq_mhz)
    if b is None: return False
    for band, lo, hi in PHONE_SUBBANDS:
        if band == b and lo <= freq_mhz <= hi:
            return True
    return False

# ---------------------- Mode normalization ----------------------
UI_MODES = ("SSB", "CW", "FT8", "RTTY", "AM", "FM", "DIGI", "OTHER")
def normalize_mode(mode_raw: Optional[str], comment: Optional[str], freq_mhz: Optional[float]) -> str:
    m = (mode_raw or "").upper()
    c = (comment or "").upper()
    blob = f"{m} {c}"
    if re.search(r"\b(USB|LSB|SSB|PHONE|PH)\b", blob): return "SSB"
    if re.search(r"\bCW\b", blob): return "CW"
    if re.search(r"\bFT8\b", blob): return "FT8"
    if re.search(r"\bRTTY\b", blob): return "RTTY"
    if re.search(r"\bAM\b", blob): return "AM"
    if re.search(r"\bFM\b", blob): return "FM"
    if re.search(r"\bFT4|PSK|JT65|WSJ[T]?|JS8|OLIVIA|MFSK|FSK31|PSK31|PACKET|HELL\b", blob): return "DIGI"
    try:
        if freq_mhz is not None and in_phone_subband(float(freq_mhz)):
            return "SSB"
    except Exception:
        pass
    return "OTHER"

# ---------------------- Prefix centroids + US call-areas ----------------------
PREFIX_CENTROIDS = {
    "K": (39.8, -98.6), "W": (39.8, -98.6), "N": (39.8, -98.6),
    "VE": (56.1, -106.3), "VA": (56.1, -106.3),
    "JA": (36.2, 138.3), "JH": (36.2, 138.3),
    "VK": (-25.3, 133.8), "ZL": (-41.8, 172.5),
    "DL": (51.2, 10.5), "F": (46.2, 2.2), "G": (53.0, -1.6),
    "I": (42.8, 12.5), "EA": (40.4, -3.7), "CT": (39.7, -8.1),
    "PY": (-14.2, -51.9), "LU": (-38.4, -63.6),
    "ZS": (-30.6, 22.9), "V5": (-22.6, 17.1),
    "OH": (64.5, 26.0), "SM": (62.8, 15.2), "OZ": (56.0, 10.0), "LA": (65.0, 11.0),
    "PA": (52.3, 5.3), "ON": (50.8, 4.4), "SP": (52.1, 19.4),
}
def guess_from_prefix(call: str) -> Tuple[float, float]:
    c = (call or "").upper()
    for p in sorted(PREFIX_CENTROIDS, key=len, reverse=True):
        if c.startswith(p):
            return PREFIX_CENTROIDS[p]
    return (0.0, 0.0)

_US_AREA_CENTROIDS = {
    "0": (39.0, -96.5),  "1": (44.0, -71.0),  "2": (42.8, -75.5),
    "3": (40.0, -77.0),  "4": (33.0, -84.0),  "5": (34.5, -97.0),
    "6": (36.5, -120.0), "7": (42.5, -112.0), "8": (41.5, -83.0),
    "9": (41.0, -89.0),
}
_US_PREFIX = re.compile(r"^(?:(?:A[A-L])|(?:K|N|W)[A-Z]?)(\d)")

def call_to_us_area_centroid(call: str) -> Optional[Tuple[float,float]]:
    m = _US_PREFIX.match((call or "").upper())
    if not m: return None
    area = m.group(1)
    return _US_AREA_CENTROIDS.get(area)

# ---------------------- Spot type ----------------------
# wherever Spot is defined
from dataclasses import dataclass

@dataclass
class Spot:
    source: str
    call: str
    freq_mhz: float
    mode: str
    time: str
    lat: float
    lon: float
    comment: str = ""
    park: str = ""   # NEW: POTA park ID like "K-1234"
    link: str = ""   # NEW: deep link to open

# ... existing Spot dataclass as above ...

# ---- Adapter to normalize dicts -> Spot ---------------------------------
from typing import Any

def _to_spot(x: Any) -> Spot:
    if isinstance(x, Spot):
        return x
    if isinstance(x, dict):
        # map common keys used by different scrapers
        return Spot(
            src=(x.get("src") or x.get("source") or ""),
            call=(x.get("call") or x.get("dx") or ""),
            freq_mhz=float(x.get("freq_mhz") or x.get("freq") or 0.0),
            mode_raw=(x.get("mode_raw") or x.get("mode") or ""),
            ts=str(x.get("ts") or x.get("time") or x.get("timestamp") or ""),
            lat=float(x.get("lat") or x.get("dx_latitude") or x.get("latitude") or 0.0),
            lon=float(x.get("lon") or x.get("dx_longitude") or x.get("longitude") or 0.0),
            comment=(x.get("comment") or x.get("info") or x.get("notes") or ""),
        )
    # last resort — keep pipeline running without crashing
    return Spot("", "", 0.0, "", "", 0.0, 0.0, "")
# -------------------------------------------------------------------------



# ---------------------- HTTP helpers ----------------------
USER_AGENT = {"User-Agent": "HF-Distance-First-Agent/4.1 (+ham)"}
def _get_text(url: str, headers: Optional[dict] = None) -> Optional[str]:
    try:
        req = Request(url, headers=(headers or USER_AGENT), method="GET")
        with urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
def _get_json(url: str, headers: Optional[dict] = None) -> Optional[dict]:
    try:
        req = Request(url, headers=(headers or USER_AGENT), method="GET")
        with urlopen(req, timeout=20) as r:
            return json.load(r)
    except Exception:
        return None

# ---------------------- Feeds: POTA / SOTA / WWFF ----------------------
def fetch_pota(limit: int = 1000):
    try:
        r = requests.get("https://api.pota.app/v1/spots", timeout=12)
        r.raise_for_status()
        data = r.json() or []
    except Exception as e:
        print(f"[POTA] fetch error: {e}")
        return []

    def _to_mhz_any(val):
        try:
            f = float(val)
        except Exception:
            m = re.search(r"(\d+(?:\.\d+)?)", str(val))
            if not m:
                return 0.0
            f = float(m.group(1))
        if f > 100_000:
            return f / 1_000_000.0  # Hz -> MHz
        if f > 1000:
            return f / 1000.0       # kHz -> MHz
        return f

    out = []
    for s in data:
        ref = (s.get("reference") or "").strip()          # e.g., "K-1234"
        call = (s.get("activator") or "").strip().upper()
        freq = _to_mhz_any(s.get("frequency"))
        mode = (s.get("mode") or "").strip().upper()
        ts = str(s.get("time") or s.get("activatorLastSpotTime") or "")
        lat = float(s.get("latitude") or 0.0)
        lon = float(s.get("longitude") or 0.0)

        if not (ref and call) or freq <= 0.0:
            continue

        comment = ref  # store park ID; link is added later when building client_rows

        out.append(Spot("POTA", call, freq, mode, ts, lat, lon, comment=comment))

        if len(out) >= limit:
            break

    return out


def fetch_sota(minutes: int = 90) -> List[Spot]:
    data = _get_json(f"https://api2.sota.org.uk/api/spots/{minutes}") or []
    out: List[Spot] = []
    for s in data:
        mode = (s.get("mode") or "")
        lat = s.get("latitude") or s.get("lat") or 0
        lon = s.get("longitude") or s.get("lon") or 0
        call = (s.get("activatorCallsign") or s.get("callsign") or "").strip()
        freq = normalize_freq_mhz(s.get("frequency"))
        if not freq: continue
        out.append(Spot("SOTA", call, freq, mode, str(s.get("timeStamp") or s.get("time") or ""),
                        float(lat or 0), float(lon or 0), (s.get("summitCode") or "")))
    return out

def fetch_wwff() -> List[Spot]:
    data = _get_json("https://wwff.co/spotline/static/spots.json") or {}
    out: List[Spot] = []
    for s in (data.get("spots") or []):
        mode = (s.get("mode") or "")
        freq = normalize_freq_mhz(s.get("frequency"))
        if not freq: continue
        out.append(Spot("WWFF", (s.get("callsign") or "").strip(), freq, mode,
                        str(s.get("time", "")), float(s.get("lat") or 0),
                        float(s.get("lon") or 0), (s.get("reference") or "")))
    return out

# ---------------------- DX Summit (JSON, robust + relay) ----------------------

DXS_DEBUG = True  # set False after it works
DXS_MODE = "scrape"   # add this near the top of your DX Summit section

def fetch_dxsummit(limit: int = 400):
    if DXS_MODE == "scrape":
        return _dxs_scrape(limit)
    # ... your existing JSON-first code, then fallback to _dxs_scrape(limit) ...


_MODE_PATTERNS = [
    (r"\bFT8\b", "FT8"),
    (r"\bFT4\b", "FT4"),
    (r"\bCW\b", "CW"),
    (r"\bSSB\b|\bLSB\b|\bUSB\b", "SSB"),
    (r"\bRTTY\b", "RTTY"),
    (r"\bPSK(?:31|63)?\b", "PSK"),
    (r"\bFM\b", "FM"),
    (r"\bAM\b", "AM"),
]
# Put HTTP endpoints first (you confirmed HTTP works)
_SCRAPE_CANDIDATES = [
    "http://www.dxsummit.fi/text/dx45.html",
    "http://www.dxsummit.fi/text/dx25.html",
    "http://www.dxsummit.fi/DxSpots.aspx?count=100&range=2",
    # HTTPS fallbacks (only if HTTP fails)
    "https://www.dxsummit.fi/text/dx45.html",
    "https://www.dxsummit.fi/text/dx25.html",
    "https://www.dxsummit.fi/DxSpots.aspx?count=100&range=2",
]

# 1) Fetch raw HTML/text
def _fetch_text(url: str) -> str:
    hdrs = {"User-Agent": "ssb_agent/1.0 (+ham waterfall)", "Accept": "text/plain, text/html"}
    try:
        r = requests.get(url, headers=hdrs, timeout=(5, 10))
        if DXS_DEBUG:
            print(f"[DXSummit] SCRAPE {url} -> {r.status_code} ({r.headers.get('Content-Type','')})")
        r.raise_for_status()
        return r.text
    except Exception as e:
        if DXS_DEBUG:
            print(f"[DXSummit] scrape failed {url}: {e}")
        return ""

# 2) Strip tags/entities to get clean lines
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE  = re.compile(r"\s+")

# --- HTML → plain text (preserve line breaks) ---
def _html_to_text(s: str) -> str:
    if not s:
        return ""
    # 1) decode entities (&amp;, &lt;, etc)
    s = _html.unescape(s)
    # 2) turn <br> (any case, with/without /) into real newlines
    s = re.sub(r'(?is)<br\s*/?>', '\n', s)
    # 3) remove <pre>, <script>, <style> wrappers but KEEP their content/newlines
    s = re.sub(r'(?is)</?pre[^>]*>', '', s)
    s = re.sub(r'(?is)<script.*?</script>', '', s)
    s = re.sub(r'(?is)<style.*?</style>',  '', s)
    # 4) strip all other tags (but do NOT collapse newlines)
    s = re.sub(r'(?is)<[^>]+>', ' ', s)
    # 5) normalize newlines; keep them!
    s = s.replace('\r\n', '\n').replace('\r', '\n')
    # 6) trim trailing spaces on each line
    s = "\n".join(line.strip() for line in s.split('\n'))
    return s

# --- Parse many lines (very permissive) ---
_DX_CALL_RE = re.compile(r"\b([A-Z0-9]{1,3}\d[A-Z0-9/]{0,10})\b")

def _scrape_parse_text(text: str, limit: int):
    out, seen = [], set()

    cleaned = _html_to_text(text)
    lines = [ln for ln in cleaned.split("\n") if ln]  # keep each line

    # Heuristic: keep only lines that look “spotty” (contain a number)
    cand = [ln for ln in lines if re.search(r"\b\d{1,5}(?:\.\d+)?\b", ln)]

    if DXS_DEBUG:
        print(f"[DXSummit] scrape lines={len(lines)}, candidates={len(cand)}")
        # Optional: print a few candidates to verify shape
        for i, ln in enumerate(cand[:5]):
            print(f"[DXSummit] cand[{i}]: {ln}")

    for ln in cand:
        U = ln.upper()
        tokens = U.split()

        # 1) find the first plausible RF frequency in MHz/kHz/Hz
        freq_mhz, fi = 0.0, -1
        for i, tok in enumerate(tokens):
            # pull a number out of the token
            m = re.search(r"(\d+(?:\.\d+)?)", tok)
            if not m:
                continue
            f = float(m.group(1))
            # normalize to MHz
            if f > 100_000: f = f / 1_000_000.0
            elif f > 1000:  f = f / 1000.0
            # 160m..microwaves guard
            if 1.5 <= f <= 3000:
                freq_mhz, fi = f, i
                break
        if freq_mhz == 0.0:
            continue

        # 2) find a DX callsign near the frequency token
        call = ""
        window = tokens[max(0, fi-4): fi+6]
        for tok in window:
            m = _DX_CALL_RE.match(tok)
            if m:
                call = m.group(1)
                break
        if not call:
            m = _DX_CALL_RE.search(U)
            if not m:
                continue
            call = m.group(1)

        # 3) Info/comment: everything after the frequency token
        info = " ".join(tokens[fi+1:])[:200]

        # 4) Optional UTC time like 2218Z
        m = re.search(r"\b(\d{3,4}Z)\b", U)
        ts = m.group(1) if m else ""

        key = (call, round(freq_mhz, 3), ts or info[:32])
        if key in seen:
            continue
        seen.add(key)

        out.append(Spot("DXS", call, freq_mhz, "", ts, 0.0, 0.0, comment=info))
        if len(out) >= limit:
            break

    return out


# 3) Parse lines permissively (no hard dependence on exact column layout)
_DX_CALL_RE = re.compile(r"\b([A-Z0-9]{1,3}\d[A-Z0-9/]{0,10})\b")

def _guess_mhz(token: str) -> float:
    try:
        f = float(token)
    except Exception:
        m = re.search(r"(\d+(?:\.\d+)?)", token)
        if not m:
            return 0.0
        f = float(m.group(1))
    if f > 100_000: return f / 1_000_000.0  # Hz -> MHz
    if f > 1000:    return f / 1000.0       # kHz -> MHz
    return f

def _scrape_parse_text(text: str, limit: int):
    out, seen = [], set()
    # Pre-clean to plain-ish text
    cleaned = _html_to_text(text)
    # Split into candidate “lines”
    lines = [ln.strip() for ln in cleaned.split("\n") if ln.strip()]
    # Heuristic: only keep lines that look like they contain a frequency
    cand = [ln for ln in lines if re.search(r"\b\d{1,5}(?:\.\d+)?\b", ln)]
    if DXS_DEBUG:
        print(f"[DXSummit] scrape candidates: {len(cand)}")

    for ln in cand:
        U = ln.upper()
        # Find a plausible frequency (first number in RF range)
        tokens = U.split()
        freq_mhz, fi = 0.0, -1
        for i, tok in enumerate(tokens):
            f = _guess_mhz(tok)
            if 1.5 <= f <= 3000:  # MHz range
                freq_mhz, fi = f, i
                break
        if freq_mhz == 0.0:
            continue
        # Find a callsign near the frequency
        call = ""
        window = tokens[max(0, fi-4): fi+5]
        for tok in window:
            m = _DX_CALL_RE.match(tok)
            if m:
                call = m.group(1)
                break
        if not call:
            m = _DX_CALL_RE.search(U)
            if not m:
                continue
            call = m.group(1)
        # Info/comment: everything after freq token
        info = " ".join(tokens[fi+1:])[:200]
        # Optional timestamp grab like 2218Z
        m = re.search(r"\b\d{3,4}Z\b", U)
        ts = m.group(0) if m else ""
        key = (call, round(freq_mhz, 3), ts or info[:30])
        if key in seen:
            continue
        seen.add(key)
        out.append(Spot("DXS", call, freq_mhz, "", ts, 0.0, 0.0, comment=info))
        if len(out) >= limit:
            break
    return out

def _dxs_scrape(limit: int):
    for url in _SCRAPE_CANDIDATES:
        txt = _fetch_text(url)
        if not txt:
            continue
        parsed = _scrape_parse_text(txt, limit)
        if parsed:
            if DXS_DEBUG:
                print(f"[DXSummit] scrape from {url} yielded {len(parsed)} spots")
            return parsed
    if DXS_DEBUG:
        print("[DXSummit] scrape produced no spots")
    return []



def _infer_mode_from_info(info: str) -> str:
    txt = (info or "").upper()
    for pat, mode in _MODE_PATTERNS:
        if re.search(pat, txt):
            return mode
    return ""

def _to_mhz(val) -> float:
    if val is None:
        return 0.0
    try:
        f = float(val)
    except Exception:
        try:
            f = float(re.findall(r"[\d.]+", str(val))[0])
        except Exception:
            return 0.0
    if f > 100_000:      # Hz
        return f / 1_000_000.0
    if f > 1000:         # kHz
        return f / 1000.0
    return f              # MHz

def _dxs_try_get(url: str, variants: list, headers: dict, timeout: int):
    """
    Try dxsummit.fi; if all fail, fall back to a read-only relay (r.jina.ai) that returns the same JSON.
    Returns (data_list, params_used_or_dict, meta_dict).
    """
    last_err = None

    sess = requests.Session()
    retry = Retry(total=4, connect=4, read=4, backoff_factor=0.8,
                  status_forcelist=(502, 503, 504), allowed_methods=("GET",),
                  raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)

    # 1) direct attempts
    for params in variants:
        try:
            r = sess.get(url, headers=headers, params=params, timeout=(5, 12))
            if DXS_DEBUG:
                print(f"[DXSummit] GET {r.url} -> {r.status_code} ({r.headers.get('Content-Type','')})")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                return data, params, {"status": r.status_code}
            if isinstance(data, list) and DXS_DEBUG:
                print("[DXSummit] empty list with params", params)
        except Exception as e:
            last_err = str(e)
            if DXS_DEBUG:
                print(f"[DXSummit] request failed with params {params}: {e}")

    # 2) relay fallback (note the double https:// in the path is intentional)
    relay_url = "https://r.jina.ai/https://www.dxsummit.fi/api/v1/spots"
    try:
        if DXS_DEBUG:
            print(f"[DXSummit] trying relay {relay_url}")
        rr = sess.get(relay_url, headers={"Accept":"application/json"}, timeout=(5, 12))
        rr.raise_for_status()
        try:
            data = rr.json()
        except Exception:
            import json
            data = json.loads(rr.text)
        if isinstance(data, list) and data:
            if DXS_DEBUG:
                print(f"[DXSummit] relay delivered {len(data)} spots")
            return data, {"relay": True}, {"status": rr.status_code}
        if DXS_DEBUG:
            print("[DXSummit] relay returned empty/unexpected payload")
    except Exception as e:
        last_err = f"{last_err} | relay error: {e}"

    return [], None, {"error": last_err}

def fetch_dxsummit(limit: int = 400):
    if DXS_MODE == "scrape":
        return _dxs_scrape(limit)
    url = "https://www.dxsummit.fi/api/v1/spots"
    headers = {
        "Accept": "application/json",
        "User-Agent": "ssb_agent/1.0 (+ham waterfall)"
    }
    param_variants = [
        {"limit_time": "true"},
        {},
        {"limit": "400"},
        {"limit_time": "true", "limit": "400"}
    ]

    out = []
    data, used, meta = _dxs_try_get(url, param_variants, headers, timeout=20)

    if DXS_DEBUG:
        if used is None:
            print(f"[DXSummit] no data from any variant. meta={meta}")
        else:
            print(f"[DXSummit] got {len(data)} spots using params={used}")

    if not data:
        return out

    for s in data:
        try:
            call = (s.get("dx_call") or "").strip().upper()
            freq_mhz = _to_mhz(s.get("frequency"))
            if not call or freq_mhz <= 0.0:
                continue

            info = s.get("info") or ""
            mode_clean = _infer_mode_from_info(info)
            ts = str(s.get("time") or "")

            lat = s.get("dx_latitude")
            lon = s.get("dx_longitude")
            if lat in (None, "", 0, 0.0) or lon in (None, "", 0, 0.0):
                grid = (s.get("dx_locator") or "").strip()
                if grid:
                    try:
                        g_lat, g_lon = maidenhead_to_latlon(grid)
                        lat, lon = g_lat, g_lon
                    except Exception:
                        pass
            try:
                lat = float(lat) if lat not in (None, "") else 0.0
                lon = float(lon) if lon not in (None, "") else 0.0
            except Exception:
                lat, lon = 0.0, 0.0

            out.append(Spot("DXS", call, freq_mhz, mode_clean or info, ts, lat, lon, comment=info))
            if len(out) >= limit:
                break
        except Exception as row_e:
            if DXS_DEBUG:
                print(f"[DXSummit] row parse skip: {row_e} | raw={s}")
            continue

    if DXS_DEBUG:
        print(f"[DXSummit] returning {len(out)} normalized spots")

    return out



# ---------------------- HamAlert (local file buffer) ----------------------
def fetch_hamalert_from_file(path: str = "hamalert_buffer.jsonl", max_items: int = 1000) -> List[Spot]:
    p = Path(path)
    if not p.exists():
        return []
    out: List[Spot] = []
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_items:]
    except Exception:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except Exception:
            continue
        call = (item.get("call") or item.get("dx") or "").strip().upper()
        freq = normalize_freq_mhz(item.get("freq") or item.get("frequency"))
        if not call or not freq:
            continue
        mode_raw = item.get("mode") or item.get("info") or item.get("comment") or ""
        ts = str(item.get("time") or item.get("ts") or "")
        lat = float(item.get("lat") or 0) if str(item.get("lat") or "").strip() else 0.0
        lon = float(item.get("lon") or 0) if str(item.get("lon") or "").strip() else 0.0
        out.append(Spot("HMA", call, freq, mode_raw, ts, lat, lon, comment=item.get("comment") or ""))
    return out

# ---------------------- DXWatch scraper (DXW) ----------------------
DXWATCH_URL = "https://www.dxwatch.com/dxsd1/dxsd1.php"
_dxrow_re = re.compile(
    r"""<tr[^>]*>\s*
        (?:<td[^>]*>.*?</td>\s*)*?
        <td[^>]*>(?P<freq>\d+(?:\.\d+)?)</td>\s*
        <td[^>]*>(?P<call>[A-Z0-9/]+)</td>\s*
        <td[^>]*>(?P<info>.*?)</td>
    """, re.IGNORECASE | re.DOTALL | re.VERBOSE)
_dxrow_alt = re.compile(
    r"""<td[^>]*class=["']?freq["']?[^>]*>\s*(?P<freq>\d+(?:\.\d+)?)\s*</td>.*?
        <td[^>]*class=["']?dxcall["']?[^>]*>\s*(?P<call>[A-Z0-9/]+)\s*</td>.*?
        <td[^>]*class=["']?remarks?["']?[^>]*>\s*(?P<info>.*?)</td>
    """, re.IGNORECASE | re.DOTALL | re.VERBOSE)

def fetch_dxwatch(limit: int = 250) -> List[Spot]:
    html_txt = _get_text(DXWATCH_URL)
    if not html_txt:
        return []
    out: List[Spot] = []
    count = 0
    def handle(freq_s: str, call_s: str, info_s: str):
        nonlocal count
        f = normalize_freq_mhz(freq_s)
        if not f: return
        call = re.sub(r"<.*?>", "", call_s).strip().upper()
        info = re.sub(r"<.*?>", "", info_s).strip()
        mode_norm = normalize_mode("", info, f)
        out.append(Spot("DXW", call, f, mode_norm, datetime.now(timezone.utc).isoformat(), 0.0, 0.0, info))
        count += 1
    for m in _dxrow_re.finditer(html_txt):
        try:
            handle(m.group("freq"), m.group("call"), m.group("info"))
            if count >= limit: break
        except Exception:
            continue
    if count == 0:
        for m in _dxrow_alt.finditer(html_txt):
            try:
                handle(m.group("freq"), m.group("call"), m.group("info"))
                if count >= limit: break
            except Exception:
                continue
    return out

# ---------------------- Regex helpers ----------------------
_POTA_LOC_CACHE: Dict[str, Tuple[float,float]] = {}
_POTA_CODE_RE = re.compile(r"\b([A-Z]{1,3}-\d{3,5})\b")
_SOTA_CODE_RE = re.compile(r"\b([A-Z]{1,2}/[A-Z]{2}-\d{3,5})\b")
_GRID_RE      = re.compile(r"\b([A-R]{2}\d{2}(?:[A-X]{2}(?:\d{2})?)?)\b", re.IGNORECASE)

def resolve_pota_latlon(park_code: str) -> Optional[Tuple[float,float]]:
    if not park_code or not re.match(r"^[A-Z]{1,3}-\d{3,5}$", park_code):
        return None
    if park_code in _POTA_LOC_CACHE:
        return _POTA_LOC_CACHE[park_code]
    candidates = [
        f"https://api.pota.app/park/{park_code}",
        f"https://api.pota.app/v1/parks/{park_code}",
        f"https://api.pota.app/park?park={park_code}",
    ]
    for url in candidates:
        data = _get_json(url)
        if not data:
            continue
        d = data if isinstance(data, dict) else (data[0] if isinstance(data, list) and data else None)
        if not isinstance(d, dict):
            continue
        lat = d.get("latitude") or d.get("lat")
        lon = d.get("longitude") or d.get("lon")
        try:
            if lat is not None and lon is not None:
                latf, lonf = float(lat), float(lon)
                _POTA_LOC_CACHE[park_code] = (latf, lonf)
                return (latf, lonf)
        except Exception:
            pass
        grid = (d.get("grid6") or d.get("grid4") or "").strip()
        if grid:
            try:
                latf, lonf = maidenhead_to_latlon(grid)
                _POTA_LOC_CACHE[park_code] = (latf, lonf)
                return (latf, lonf)
            except Exception:
                pass
    return None

def resolve_sota_latlon(summit_code: str) -> Optional[Tuple[float,float]]:
    if not summit_code or "/" not in summit_code: return None
    data = _get_json(f"https://api2.sota.org.uk/api/summits/{summit_code}")
    if not isinstance(data, dict): return None
    try:
        lat = float(data.get("latitude") or data.get("lat"))
        lon = float(data.get("longitude") or data.get("lon"))
        return (lat, lon)
    except Exception:
        return None

def extract_program_code(comment: str, mode_raw: str) -> Tuple[Optional[str], Optional[str]]:
    blob = " ".join([(comment or ""), (mode_raw or "")])
    m_pota = _POTA_CODE_RE.search(blob)
    m_sota = _SOTA_CODE_RE.search(blob)
    return (m_pota.group(1) if m_pota else None, m_sota.group(1) if m_sota else None)
def extract_grid(blob: str) -> Optional[str]:
    m = _GRID_RE.search(blob or "")
    return m.group(1).upper() if m else None

# ---------------------- VOACAP (optional) ----------------------
@dataclass
class Antenna:
    name: str
    height_m: float
ANTENNAS_BY_BAND: Dict[int, Antenna] = {7: Antenna("DIPOLE", DIPOLE_H_40M), 14: Antenna("DIPOLE", DIPOLE_H_20M)}
REQ_SNR_SSB_DBHZ = 45.0
def have_voacap() -> bool: return shutil.which("voacapl") is not None
VOACAP_TEMPLATE = """COMMENT Auto HF-Distance-First-Agent
LINEMAX 55
COEFFS CCIR
TIME 1 24 1 1
MONTH {year} {month:.2f}
SUNSPOT {ssn:.1f}
TXLOC {tx_lat:.2f} {tx_lon:.2f}
RXLOC {rx_lat:.2f} {rx_lon:.2f}
SYSTEM 0.0 0.0
SNR {req_snr:.1f}
RELIABILITY
FREQUENCY {freq_mhz:.2f}
TXPOW {tx_power_dbw:.1f}
ANTENNA TX {ant_name} {ant_height_m:.1f}
ANTENNA RX DIPOLE 10.0
METHOD 30
"""
def run_voacap_rel(tx_lat, tx_lon, rx_lat, rx_lon, freq_mhz, req_snr_dbhz, ant, ssn=90.0) -> Optional[float]:
    if not have_voacap(): return None
    tx_power_dbw = 10 * math.log10(max(MY_POWER_W, 1e-9))
    now = datetime.now(timezone.utc)
    content = VOACAP_TEMPLATE.format(
        year=now.year, month=now.month + 0.0, ssn=ssn,
        tx_lat=tx_lat, tx_lon=tx_lon, rx_lat=rx_lat, rx_lon=rx_lon,
        req_snr=req_snr_dbhz, freq_mhz=freq_mhz, tx_power_dbw=tx_power_dbw,
        ant_name=ant.name, ant_height_m=ant.height_m,
    )
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "voacapx.dat"; out = Path(td) / "out"
        inp.write_text(content)
        try:
            subprocess.run(["voacapl", str(inp), str(out)], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            return None
        rel_path = out / "REL.DAT"
        if not rel_path.exists(): return None
        lines = rel_path.read_text(errors="ignore").strip().splitlines()
        try:
            vals = [float(x) for x in re.findall(r"[-+]?[0-9]*\.?[0-9]+", lines[-1])]
            if len(vals) >= 24:
                rel_pct = vals[datetime.now(timezone.utc).hour]
                return max(0.0, min(1.0, rel_pct/100.0))
        except Exception:
            return None
    return None

# ---------------------- Scoring (distance + propagation) × freshness ----------------------
DIST_WEIGHT = 0.60
PROP_WEIGHT = 0.40

# Freshness
HALF_LIFE_S = 45 * 60          # 45 min half-life
MAX_AGE_HOURS = 6              # drop anything older than this

def time_freshness_factor(age_seconds: float) -> float:
    if age_seconds <= 0:
        return 1.0
    # exponential decay: factor = 2^(-age / half_life)
    return 2.0 ** ( - age_seconds / max(1.0, HALF_LIFE_S) )

DIST_PREF = {
    160: (400, 600),
    80:  (600, 800),
    60:  (800, 900),
    40:  (1200, 1200),
    30:  (1500, 1300),
    20:  (2500, 1800),
    17:  (3000, 2000),
    15:  (3500, 2200),
    12:  (4000, 2400),
    10:  (4500, 2600),
}
def distance_suitability(band_m: int, dist_mi: Optional[float]) -> float:
    if not dist_mi or dist_mi <= 0: return 0.5
    center, sigma = DIST_PREF.get(band_m, (2000, 2000))
    x = (dist_mi - center) / max(1.0, sigma)
    return max(0.0, min(1.0, math.exp(-(x*x))))

def propagation_factor(freq_mhz: float, rx_lat: float, rx_lon: float) -> float:
    band = freq_to_meters(freq_mhz) or round(freq_mhz)
    ant = ANTENNAS_BY_BAND.get(band) or Antenna("DIPOLE", 10.0)
    rel = run_voacap_rel(MY_LAT, MY_LON, rx_lat, rx_lon, freq_mhz, REQ_SNR_SSB_DBHZ, ant)
    if rel is not None:
        return max(0.0, min(1.0, float(rel)))
    hr = datetime.now(timezone.utc).hour
    day = 7 <= hr <= 18
    bias = {1: not day,2: not day,3: not day,5: not day,7: not day,10: True,14: True,18: day,21: day,24: day,28: day}.get(band, True)
    return 0.9 if bias else 0.6

def combined_score(band_m: int, dist_mi: Optional[float], freq_mhz: float, rx_lat: float, rx_lon: float, age_seconds: float) -> float:
    d = distance_suitability(band_m, dist_mi)
    p = propagation_factor(freq_mhz, rx_lat, rx_lon)
    base = max(0.0, min(1.0, DIST_WEIGHT * d + PROP_WEIGHT * p))
    freshness = time_freshness_factor(age_seconds)
    return max(0.0, min(1.0, base * freshness))

# ---------------------- Timestamp parsing ----------------------
_ISO_Z_RE = re.compile(r"Z$")
_MS_WRAP_RE = re.compile(r"/Date\((\d+)\)/")

def parse_ts_to_dt(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    s = str(ts).strip()
    # /Date(1693434132000)/
    m = _MS_WRAP_RE.match(s)
    if m:
        try:
            ms = int(m.group(1))
            return datetime.fromtimestamp(ms/1000.0, tz=timezone.utc)
        except Exception:
            return None
    # pure digits: seconds or milliseconds
    if s.isdigit():
        try:
            iv = int(s)
            if len(s) >= 13:  # ms
                return datetime.fromtimestamp(iv/1000.0, tz=timezone.utc)
            else:             # seconds
                return datetime.fromtimestamp(iv, tz=timezone.utc)
        except Exception:
            return None
    # ISO-ish
    try:
        ss = _ISO_Z_RE.sub("+00:00", s)  # replace Z with +00:00
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    # Fallbacks: try common formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue
    return None

def seconds_since(ts: str, now: Optional[datetime]=None) -> Optional[float]:
    now = now or datetime.now(timezone.utc)
    dt = parse_ts_to_dt(ts)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds())

# ---------------------- Source links ----------------------
def source_link(src: str, comment: str) -> str:
    c = (comment or "").strip()
    if src == "POTA":
        if re.match(r"^[A-Z]{1,3}-\d{3,5}$", c): return f"https://pota.app/#/park/{c}"
        return "https://pota.app/#/spots"
    if src == "SOTA":
        if "/" in c and "-" in c: return f"https://summits.sota.org.uk/summit/{c}"
        return "https://sotawatch.sota.org.uk/"
    if src == "WWFF":
        return f"https://wwff.co/?s={quote_plus(c)}" if c else "https://wwff.co/"
    if src == "DXW":
        return "https://www.dxwatch.com/dxsd1/dxsd1.php"
    if src == "DXS":
        return "https://www.dxsummit.fi/"
    if src == "HMA":
        return "https://hamalert.org/"
    return ""

# ---------------------- HTML (table + filters + waterfall) ----------------------
def build_html(rows: List[Dict], display_bands: Optional[Set[int]] = None,
               display_modes: Optional[Set[str]] = None, refresh_seconds: int = 60,
               source_counts: Dict[str,int] = None, rows_json_name: str = "rows.json") -> str:
    source_counts = source_counts or {}

    present_bands = sorted({int(r["band"]) for r in rows if r.get("band")}) or [160,80,60,40,30,20,17,15,12,10]
    present_modes = sorted({r["mode"] for r in rows if r.get("mode")}) or list(UI_MODES)
    initial_bands = sorted(display_bands) if display_bands else present_bands
    initial_modes = sorted(display_modes) if display_modes else ["SSB","OTHER"]
    client_rows = []
    for r in rows:
        src = r.get("src", "")

        # carry through base fields
        band    = int(r["band"]) if r.get("band") is not None else None
        freq    = float(r["freq"])
        call    = r.get("call", "")
        mode    = r.get("mode", "")
        score   = float(r.get("score", 0))
        link    = r.get("link", "")
        comment = r.get("comment", "")
        dist_mi = float(r["dist_mi"]) if r.get("dist_mi") is not None else None
        geo     = r.get("geo", "")

        # POTA enrichment: set park ID into geo and synthesize link if missing
        if src == "POTA":
            # prefer explicit park field if you have one; else try geo/comment
            txt = r.get("park") or geo or comment
            m = re.search(r"\b[A-Z]{1,3}-\d{1,5}\b", str(txt))
            if m:
                ref = m.group(0)
                geo = ref  # show park ID in the existing "Geo" column

        client_rows.append({
            "band": band,
            "freq": freq,
            "call": call,
            "src": src,
            "mode": mode,
            "score": score,
            "link": link,
            "comment": comment,
            "dist_mi": dist_mi,
            "geo": geo,
        })



    # Build the client rows JSON for the browser
    data_js = json.dumps(client_rows).replace("</", "<\\/")

    # Human-readable counts string for the header
    counts_html = " • ".join(
        f"{k}:{source_counts.get(k, 0)}"
        for k in ("POTA", "SOTA", "WWFF", "DXW", "DXS", "HMA")
    )

    head = f"""<!doctype html><html><head><meta charset="utf-8">
<title>HF Live Spots (Distance-first + Freshness + Waterfall)</title>
<style>
body{{font-family:system-ui;margin:20px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ddd;padding:6px}} th{{background:#f5f5f5;position:sticky;top:0}}
td{{white-space:nowrap}}
.ok{{padding:2px 6px;border-radius:6px;background:#e7f7e7;border:1px solid #b7e6b7}}
.bad{{padding:2px 6px;border-radius:6px;background:#fdeaea;border:1px solid #f5c2c2}}
.small{{color:#666;font-size:12px}}
.controls{{margin:8px 0; display:grid; grid-template-columns: auto 1fr auto 1fr auto; gap:10px; align-items:center}}
select[multiple]{{min-width:220px; max-width:360px; height:120px}}
button{{padding:6px 10px}}
canvas{{image-rendering:pixelated}}
.axis{{height:22px}}
.tip{{position:fixed;display:inline-block;background:rgba(0,0,0,0.92);color:#fff;padding:6px 8px;border-radius:2px;
     font-size:12px;line-height:1.25;box-shadow:0 2px 10px rgba(0,0,0,0.25);white-space:nowrap;pointer-events:auto;z-index:9999;
     text-decoration:none;cursor:pointer}}
.tip.disabled{{pointer-events:none;cursor:default}}
.tip strong{{font-weight:700}}
.hidden{{display:none}}
.btnrow{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:4px}}
.btnrow span{{font-size:12px;color:#555;margin-right:6px}}
.wfwrap{{position:relative}}
#wfgrid{{position:absolute;left:0;top:0;pointer-events:none}}
</style>
</head><body>"""

    # Build the client rows JSON and counts string
    data_js = json.dumps(client_rows).replace("</", "<\\/")
    counts_html = " • ".join(
        f"{k}:{source_counts.get(k, 0)}"
        for k in ("POTA", "SOTA", "WWFF", "DXW", "DXS", "HMA")
    )

    controls = [
        f"<h1>HF Live Spots — {html.escape(MY_GRID)}</h1>",
        (
            f"<div class='small'>Now {datetime.now().isoformat(timespec='seconds')} • "
            f"Poll: {refresh_seconds}s • Sources: {html.escape(counts_html)} • "
            f"Weights: distance {int(DIST_WEIGHT*100)}% / propagation {int(PROP_WEIGHT*100)}% × freshness</div>"
        ),
        '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px">'
        '<button id="zoomIn" type="button">Zoom In</button>'
        '<button id="zoomOut" type="button">Zoom Out</button>'
        '<button id="zoomReset" type="button">Reset</button>'
        '<button id="panLeft" type="button">← Pan</button>'
        '<button id="panRight" type="button">Pan →</button>'
        '<span id="pollStatus" class="small" style="margin-left:10px">poll: —</span>'
        '</div>',
        # Waterfall + grid
        '<div class="wfwrap">'
        '<canvas id="wf" width="1200" height="260" style="width:100%;height:260px;border:1px solid #ccc;display:block;margin:6px 0 2px 0;cursor:grab"></canvas>'
        '<canvas id="wfgrid" width="1200" height="260" style="width:100%;height:260px;"></canvas>'
        '<canvas id="wfax" class="axis" width="1200" height="22" style="width:100%;border:1px solid #eee;border-top:none;display:block;margin:0 0 10px 0"></canvas>'
        '</div>',
        '<a id="tip" class="tip hidden" href="#" target="_blank" rel="noopener"></a>',
        '<div id="wfinfo" class="small"></div>',
        # Filters + table
        "<div class='controls'>"
        "<label for='bandselect'><strong>Bands:</strong></label>"
        "<select id='bandselect' multiple></select>"
        "<label for='modeselect'><strong>Modes:</strong></label>"
        "<select id='modeselect' multiple></select>"
        "<div>"
        "<button id='allbands' type='button'>All bands</button> "
        "<button id='nobands' type='button'>No bands</button><br>"
        "<button id='allmodes' type='button'>All modes</button> "
        "<button id='nomodes' type='button'>No modes</button>"
        "</div>"
        "</div>",
        # Table header — includes Link column
        "<table id='pickstable'><thead><tr>"
        "<th>Freq (MHz)</th><th>Band</th><th>Mode</th><th>Call</th><th>Program</th>"
        "<th>Distance (mi)</th><th>Score</th><th>Geo</th><th>Link</th><th>Advice</th>"
        "</tr></thead><tbody></tbody></table>",
        # Bootstrap data/config for client
        (
            f"<script>let ROWS={data_js}; const ROWS_JSON='{rows_json_name}'; "
            f"const PRESENT_BANDS={json.dumps(present_bands)}; "
            f"const PRESENT_MODES={json.dumps(present_modes)}; "
            f"const INITIAL_BANDS={json.dumps(initial_bands)}; "
            f"const INITIAL_MODES={json.dumps(initial_modes)}; "
            f"const POLL_MS={refresh_seconds*1000};</script>"
        ),
        # Main JS — keep this as ONE raw triple-quoted string
        r"""<script>
(function(){
  const tbody       = document.querySelector('#pickstable tbody');
  const bandSelect  = document.getElementById('bandselect');
  const modeSelect  = document.getElementById('modeselect');
  const wf          = document.getElementById('wf');
  const wfgrid      = document.getElementById('wfgrid');
  const wfax        = document.getElementById('wfax');
  const tip         = document.getElementById('tip');
  const wfi         = document.getElementById('wfinfo');
  const statusEl    = document.getElementById('pollStatus');
  const gfx         = wf.getContext('2d');
  const gridfx      = wfgrid.getContext('2d');
  const ax          = wfax.getContext('2d');

  function selectedFrom(sel, mapToInt=false){
    const s = new Set();
    for(const o of sel.options){ if(o.selected){ s.add(mapToInt?parseInt(o.value):o.value); } }
    return s;
  }
  function renderSelect(sel, items, initial){
    sel.innerHTML = '';
    const init = new Set(initial);
    for(const it of items){
      const opt = document.createElement('option');
      opt.value = String(it);
      opt.textContent = String(it) + (sel===modeSelect ? '' : 'm');
      opt.selected = init.has(it);
      sel.appendChild(opt);
    }
  }

  // Table renderer — expects r.link and r.geo already populated
  function renderTable(){
    tbody.innerHTML = '';
    const vb = selectedFrom(bandSelect, true);
    const vm = selectedFrom(modeSelect, false);
    let rows = (Array.isArray(ROWS)?ROWS:[]).filter(r => (r.band && vb.has(parseInt(r.band))) && (r.mode && vm.has(r.mode)));
    rows.sort((a,b)=>{
      const aHi = Number(a.score)>=0.85, bHi = Number(b.score)>=0.85;
      if (aHi!==bHi) return aHi? -1: 1;
      if (b.score!==a.score) return Number(b.score)-Number(a.score);
      const ad = (a.dist_mi==null?Number.POSITIVE_INFINITY:Number(a.dist_mi));
      const bd = (b.dist_mi==null?Number.POSITIVE_INFINITY:Number(b.dist_mi));
      return ad-bd;
    });
    if(!rows.length){
      const tr = document.createElement('tr');
      tr.innerHTML = '<td colspan="10" style="color:#666;padding:12px">No spots for the selected bands/modes.</td>';
      tbody.appendChild(tr);
      return;
    }
    for(const r of rows){
      const tr = document.createElement('tr');
      const isHi = Number(r.score) >= 0.85;
      const advice = isHi ? '<span class="ok">Call now ★</span>' : (r.score < 0.40 ? '<span class="bad">Low odds</span>' : '');
      const scoreCell = (isHi ? '<strong>' : '') + Number(r.score).toFixed(2) + (isHi ? '</strong>' : '');
      const d = (r.dist_mi==null)?'—':Math.round(Number(r.dist_mi)).toString();
      tr.innerHTML =
        '<td>'+Number(r.freq).toFixed(3)+'</td>'+
        '<td>'+r.band+'m</td>'+
        '<td>'+r.mode+'</td>'+
        '<td>'+(r.call||'—')+'</td>'+
        '<td>'+r.src+'</td>'+
        '<td>'+d+'</td>'+
        '<td>'+scoreCell+'</td>'+
        '<td>'+(r.geo||'')+'</td>'+
        '<td>'+(r.link?'<a href="'+r.link+'" target="_blank" rel="noopener">open</a>':'')+'</td>'+
        '<td>'+advice+'</td>';
      tbody.appendChild(tr);
    }
  }

  // ... your other JS (waterfall, tick, etc) ...

  // Polling hookup at the end:
  setInterval(tick, TICK_MS);

  // Filters
  function refreshForFilterChange(){
    renderTable();
    // Repaint latest row with filters (no need to clear whole history)
    markers = [];
    gfx.fillStyle = 'rgba(255,255,255,1)';
    gfx.fillRect(0, 0, wf.width, 1);
    paintNewestRow();
  }
  bandSelect.addEventListener('change', refreshForFilterChange);
  modeSelect.addEventListener('change', refreshForFilterChange);

  document.getElementById('allbands').addEventListener('click', ()=>{ for(const o of bandSelect.options) o.selected=true; refreshForFilterChange(); });
  document.getElementById('nobands').addEventListener('click', ()=>{ for(const o of bandSelect.options) o.selected=false; refreshForFilterChange(); });
  document.getElementById('allmodes').addEventListener('click', ()=>{ for(const o of modeSelect.options) o.selected=true; refreshForFilterChange(); });
  document.getElementById('nomodes').addEventListener('click', ()=>{ for(const o of modeSelect.options) o.selected=false; refreshForFilterChange(); });
})();
</script>"""
    ]

    # Emit final HTML
    html_out = head + "\n".join(controls) + "\n</body></html>"
    Path(out_path).write_text(html_out, encoding="utf-8")

# ---------------------- Pipeline ----------------------
def gather_spots(no_dxwatch: bool = False):
    # Initialize counts and containers FIRST so we never hit NameError
    counts = {'POTA': 0, 'SOTA': 0, 'WWFF': 0, 'DXW': 0, 'DXS': 0, 'HMA': 0}
    pota, sota, wwff, dxw, dxs, hma = [], [], [], [], [], []

    # --- POTA ---
    try:
        pota = fetch_pota()
    except Exception as e:
        print(f"[POTA] error: {e}")
        pota = []
    counts['POTA'] = len(pota)

    # --- SOTA ---
    try:
        sota = fetch_sota()
    except Exception as e:
        print(f"[SOTA] error: {e}")
        sota = []
    counts['SOTA'] = len(sota)

    # --- WWFF ---
    try:
        wwff = fetch_wwff()
    except Exception as e:
        print(f"[WWFF] error: {e}")
        wwff = []
    counts['WWFF'] = len(wwff)

    # --- DXWatch (optional) ---
    try:
        dxw = [] if no_dxwatch else fetch_dxwatch()
    except Exception as e:
        print(f"[DXWatch] error: {e}")
        dxw = []
    counts['DXW'] = len(dxw)

    # --- DX Summit (uses your JSON/scrape implementation) ---
    try:
        dxs = fetch_dxsummit()
        # temporary debug so you can see what we fetched
        print(f"[DXSummit] dxs fetched = {len(dxs)}")
    except Exception as e:
        print(f"[DXSummit] disabled this cycle: {e}")
        dxs = []
    counts['DXS'] = len(dxs)

    # --- HamAlert (disabled until wired) ---
    hma = []  # leave as [] for now
    counts['HMA'] = 0

    # Return combined list and counts
    return pota + sota + wwff + dxw + dxs + hma, counts



def process(spots: List[Spot]) -> List[Dict]:
    spots = [_to_spot(s) for s in spots]
    rows: List[Dict] = []
    now = datetime.now(timezone.utc)
    max_age_s = MAX_AGE_HOURS * 3600

    for s in spots:
        # ---- Normalize fields ONCE and reuse below ----
        src      = getattr(s, "src", None) or getattr(s, "source", "") or ""
        comment  = getattr(s, "comment", "") or getattr(s, "remarks", "") or ""
        mode_raw = getattr(s, "mode_raw", None) or getattr(s, "mode", "") or ""

        # freq may be in MHz (float) or Hz (int) depending on scraper
        freq_mhz = getattr(s, "freq_mhz", None)
        if freq_mhz is None:
            f_hz = getattr(s, "frequency", None)
            if f_hz is not None:
                try:
                    freq_mhz = float(f_hz) / 1e6
                except Exception:
                    freq_mhz = None

        band = freq_to_meters(freq_mhz)
        if band is None:
            continue


                # ---- SAFE MODE / FREQ extraction ----
        mode_raw = getattr(s, "mode_raw", None) or getattr(s, "mode", "") or ""
        comment  = getattr(s, "comment", "") or getattr(s, "remarks", "") or ""

        freq_mhz = getattr(s, "freq_mhz", None)
        if freq_mhz is None:
            f_hz = getattr(s, "frequency", None)
            if f_hz is not None:
                try:
                    freq_mhz = float(f_hz) / 1e6
                except Exception:
                    freq_mhz = None

        mode_norm = normalize_mode(mode_raw, comment, freq_mhz)


# --- Age / freshness (robust to missing ts) ---
        ts_for_age = getattr(s, "ts", None)
        if ts_for_age is None:
            # Try common alternatives from scrapers
            ts_for_age = (
                getattr(s, "time_utc", None)
                or getattr(s, "timestamp_utc", None)
                or getattr(s, "timestamp", None)
                or getattr(s, "datetime", None)
                or getattr(s, "dt", None)
            )
            # Normalize types to aware UTC datetime
            if isinstance(ts_for_age, (int, float)):  # epoch seconds
                ts_for_age = datetime.fromtimestamp(ts_for_age, tz=timezone.utc)
            elif isinstance(ts_for_age, str):         # ISO8601
                t = ts_for_age.rstrip("Z")
                try:
                    ts_for_age = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
                except Exception:
                    ts_for_age = None
            elif ts_for_age is not None and getattr(ts_for_age, "tzinfo", None) is None:
                # naive -> assume UTC
                try:
                    ts_for_age = ts_for_age.replace(tzinfo=timezone.utc)
                except Exception:
                    ts_for_age = None

        age_s = seconds_since(ts_for_age, now)  # None if unknown

        if age_s is not None and age_s > max_age_s:
            continue  # drop stale spots entirely

        rx_lat, rx_lon = s.lat, s.lon
        geo_used = ""

        if abs(rx_lat) < 1e-6 and abs(rx_lon) < 1e-6:
            pota_code, sota_code = extract_program_code(comment, mode_raw)
            if src == "POTA" or pota_code:
                loc = resolve_pota_latlon(pota_code or comment)
                if loc: rx_lat, rx_lon = loc; geo_used = "pota.park"
            if (abs(rx_lat) < 1e-6 and abs(rx_lon) < 1e-6) and (src == "SOTA" or sota_code):
                loc = resolve_sota_latlon(sota_code or comment)
                if loc: rx_lat, rx_lon = loc; geo_used = "sota.summit"
            if abs(rx_lat) < 1e-6 and abs(rx_lon) < 1e-6:
                grid = extract_grid(" ".join([comment or "", mode_raw or ""]))
                if grid:
                    try: rx_lat, rx_lon = maidenhead_to_latlon(grid); geo_used="grid"
                    except Exception: pass
            if abs(rx_lat) < 1e-6 and abs(rx_lon) < 1e-6 and s.call:
                us_area = call_to_us_area_centroid(s.call)
                if us_area: rx_lat, rx_lon = us_area; geo_used="us.area"
            if abs(rx_lat) < 1e-6 and abs(rx_lon) < 1e-6 and s.call:
                rx_lat, rx_lon = guess_from_prefix(s.call)
                if abs(rx_lat) >= 1e-6 or abs(rx_lon) >= 1e-6:
                    geo_used = f"prefix:{s.call[:2].upper().rstrip('0123456789')}"
            if abs(rx_lat) < 1e-6 and abs(rx_lon) < 1e-6:
                rx_lat, rx_lon = 0.0, 0.0
                if not geo_used: geo_used = "none"

        dist_mi = None
        if abs(rx_lat) >= 1e-6 and abs(rx_lon) >= 1e-6:
            dist_mi = haversine_miles(MY_LAT, MY_LON, rx_lat, rx_lon)

        # If timestamp unknown, treat as fresh (age_s=None => factor=1.0)
        freshness_s = 0.0 if age_s is None else age_s
        score = combined_score(band, dist_mi, float(freq_mhz or 0.0), rx_lat or MY_LAT, rx_lon or MY_LON, freshness_s)
        link = source_link(src, comment)

        print(f"[dbg] process() produced {len(rows)} rows; sample={rows[:2]}")

        # build ISO UTC timestamp if possible
        dt = extract_timestamp(s)
        time_utc = dt.isoformat().replace("+00:00", "Z") if dt else None

        rows.append({
            "band": band,
            "freq": float(s.freq_mhz),
            "call": s.call,
            "src": src,
            "mode": mode_norm,
            "score": score,
            "link": link,
            "comment": comment,
            "dist_mi": dist_mi,
            "geo": geo_used or ("pota.latlon" if (src == "POTA" and (s.lat or s.lon)) else ""),
            # NEW: coordinates of the DX/activator (what we computed as rx_lat/rx_lon)
            "lat":  (rx_lat if abs(rx_lat) >= 1e-6 else None),
            "lon":  (rx_lon if abs(rx_lon) >= 1e-6 else None),
            # NEW: your station/QTH coords (derived from MY_GRID)
            "my_lat": MY_LAT,
            "my_lon": MY_LON,
            # NEW: canonical timestamp
            "time_utc": time_utc,
        })


    # De-dupe by (call, freq, mode) — keep best score
    uniq: Dict[Tuple[str, float, str], Dict] = {}
    for r in rows:
        k = (r["call"], round(r["freq"], 3), r["mode"])
        if k not in uniq or r["score"] > uniq[k]["score"]:
            uniq[k] = r

    # Sort stable (band asc, distance asc, score desc)
    return sorted(
        uniq.values(),
        key=lambda x: (x["band"], (x["dist_mi"] if x["dist_mi"] is not None else 1e9), -x["score"])
    )[:800]

def _fallback_html(rows, counts, refresh_seconds, note="html_out was None"):
    meta = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh_seconds else ""
    rows_count = len(rows) if rows is not None else 0
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>HAM UI</title>
  {meta}
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:20px}}
    .muted{{color:#666}}
    table{{border-collapse:collapse;width:100%}}
    th,td{{border:1px solid #ddd;padding:6px;font-size:14px}}
    th{{background:#f5f5f5;text-align:left}}
  </style>
</head>
<body>
  <h1>HAM UI</h1>
  <p class="muted">{note}</p>
  <p>Spots: {rows_count} — Counts: {counts}</p>
</body>
</html>"""




# ---------------------- Writer (HTML + rows.json) ----------------------
def build_html_and_rows(output_path: Path, rows: List[Dict], counts: Dict[str,int],
                        display_bands: Optional[Set[int]], display_modes: Optional[Set[str]],
                        refresh_seconds: int) -> None:
    rows_path = output_path.with_suffix('.rows.json')
    rows_payload = [{
        "band": int(r["band"]) if r.get("band") else None,
        "freq": float(r["freq"]),
        "call": r["call"],
        "src": r["src"],
        "mode": r["mode"],
        "score": float(r["score"]),
        "link": r.get("link",""),
        "comment": r.get("comment",""),
        "dist_mi": r.get("dist_mi"),
        "geo": r.get("geo",""),
        # NEW:
        "lat": r.get("lat"),
        "lon": r.get("lon"),
        "my_lat": r.get("my_lat"),
        "my_lon": r.get("my_lon"),
        "time_utc": r.get("time_utc"),
    } for r in rows]


    # Ensure output directory exists (prevents write errors if folder missing)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    rows_path.write_text(json.dumps(rows_payload), encoding="utf-8")

    # Call your existing renderer
    html_out = build_html(rows, display_bands, display_modes, refresh_seconds, counts,
                          rows_json_name=rows_path.name)

    # Guard: fallback if renderer returned None or non-str
    if not isinstance(html_out, str):
        try:
            import sys
            print("[warn] build_html returned non-str/None; writing fallback HTML", file=sys.stderr)
        except Exception:
            pass
        html_out = _fallback_html(rows, counts, refresh_seconds)

    output_path.write_text(html_out, encoding="utf-8")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] wrote {output_path} and {rows_path}  counts={counts}")


import sqlite3
from contextlib import closing
from db_paths import spots_db_path


def ensure_db(db_path: str) -> None:
    with closing(sqlite3.connect(spots_db_path())) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL;")

        # If 'spots' is missing, create fresh with the new schema
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='spots';")
        exists = cur.fetchone() is not None

        if not exists:
            conn.execute("""
            CREATE TABLE spots (
                id INTEGER PRIMARY KEY,
                src TEXT,
                call TEXT,
                freq_mhz REAL,
                mode_raw TEXT,
                ts TEXT,
                lat REAL,
                lon REAL,
                comment TEXT,
                inserted_utc TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                UNIQUE (src, call, freq_mhz, ts) ON CONFLICT IGNORE
            );
            """)
            return

        # If it exists, check columns
        cols = {row[1] for row in conn.execute("PRAGMA table_info(spots);").fetchall()}
        needed = {"src","call","freq_mhz","mode_raw","ts","lat","lon","comment","inserted_utc"}
        if "src" in cols and needed.issubset(cols):
            return  # up to date

        # Otherwise: migrate (create spots_new, copy, drop, rename)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS spots_new (
            id INTEGER PRIMARY KEY,
            src TEXT,
            call TEXT,
            freq_mhz REAL,
            mode_raw TEXT,
            ts TEXT,
            lat REAL,
            lon REAL,
            comment TEXT,
            inserted_utc TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE (src, call, freq_mhz, ts) ON CONFLICT IGNORE
        );
        """)

        # Build a portable INSERT … SELECT based on what columns we have
        # Try to map 'source'->'src' if present; else use empty string
        has_source = "source" in cols
        insert_sql = """
            INSERT INTO spots_new (src, call, freq_mhz, mode_raw, ts, lat, lon, comment, inserted_utc)
            SELECT {src_expr}, 
                   COALESCE(call,''), 
                   COALESCE(freq_mhz,0.0), 
                   COALESCE(mode_raw,''), 
                   COALESCE(ts,''), 
                   COALESCE(lat,0.0), 
                   COALESCE(lon,0.0), 
                   COALESCE(comment,''), 
                   COALESCE(inserted_utc, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            FROM spots;
        """.format(src_expr=("COALESCE(source,'')" if has_source else "''"))
        conn.execute("BEGIN;")
        conn.execute(insert_sql)
        conn.execute("DROP TABLE spots;")
        conn.execute("ALTER TABLE spots_new RENAME TO spots;")
        conn.execute("COMMIT;")
        conn.execute("VACUUM;")


def upsert_spots(db_path: str, spots: list) -> int:
    """Insert-or-ignore raw spots. Returns inserted row count."""
    if not spots:
        return 0
    with closing(sqlite3.connect(spots_db_path())) as conn, conn:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        sql = """INSERT OR IGNORE INTO spots
                 (src, call, freq_mhz, mode_raw, ts, lat, lon, comment)
                 VALUES (?,?,?,?,?,?,?,?)"""
        inserted = 0
        for s in spots:
            # be robust to dicts or Spot objects
            src      = getattr(s, "src", None) or getattr(s, "source", "") or ""
            call     = getattr(s, "call", "") or ""
            mode_raw = getattr(s, "mode_raw", None) or getattr(s, "mode", "") or ""
            ts       = getattr(s, "ts", None) or getattr(s, "time_utc", None) \
                        or getattr(s, "timestamp_utc", None) or getattr(s, "timestamp", None) \
                        or getattr(s, "datetime", None) or getattr(s, "dt", None) or ""
            # freq can be in MHz or Hz
            freq_mhz = getattr(s, "freq_mhz", None)
            if freq_mhz is None:
                f_hz = getattr(s, "frequency", None)
                if f_hz is not None:
                    try: freq_mhz = float(f_hz) / 1e6
                    except Exception: freq_mhz = None
            if freq_mhz is None:
                try: freq_mhz = float(getattr(s, "freq", 0.0))
                except Exception: freq_mhz = 0.0

            lat = getattr(s, "lat", 0.0) or 0.0
            lon = getattr(s, "lon", 0.0) or 0.0
            comment = getattr(s, "comment", "") or getattr(s, "remarks", "") or ""

            try:
                cur.execute(sql, (str(src), str(call), float(freq_mhz or 0.0),
                                  str(mode_raw), str(ts), float(lat), float(lon), str(comment)))
                if cur.rowcount == 1:
                    inserted += 1
            except Exception as e:
                # don't bomb the whole batch; skip bad rows
                print(f"[warn] upsert skip ({e}) src={src} call={call} freq_mhz={freq_mhz}")
        return inserted




# ---------------------- Build/Serve ----------------------
# ---------------------- Build/Serve ----------------------
def build_and_write(output_path: Path, no_dxwatch: bool, display_bands: Optional[Set[int]],
                    display_modes: Optional[Set[str]], refresh_seconds: int) -> None:
    spots, counts = gather_spots(no_dxwatch=no_dxwatch)

    # NEW: ship spots to Postgres via API (non-fatal)
    post_spots_live(spots)

    # Optional: keep SQLite persistence if you still want it locally
    db_path = str(spots_db_path())  # honors SPOTS_DB if set, else repo_root/data/spots.sqlite
    ensure_db(db_path)
    inserted = upsert_spots(db_path, spots)

    rows = process(spots)
    post_snapshot(rows)  
    print(f"[dbg] build: rows={len(rows)}  counts={counts}")
    build_html_and_rows(output_path, rows, counts, display_bands, display_modes, refresh_seconds)



def serve_loop(output_path: Path, refresh_seconds: int, iterations: int, no_dxwatch: bool,
               display_bands: Optional[Set[int]], display_modes: Optional[Set[str]]) -> None:
    if refresh_seconds <= 0: refresh_seconds = 60
    if iterations == 0:
        while True:
            try: build_and_write(output_path, no_dxwatch, display_bands, display_modes, refresh_seconds)
            except Exception as e: print(f"Error: {e}")
            time.sleep(refresh_seconds)
    else:
        for i in range(iterations):
            try: build_and_write(output_path, no_dxwatch, display_bands, display_modes, refresh_seconds)
            except Exception as e: print(f"Error: {e}")
            if i < iterations - 1: time.sleep(refresh_seconds)

# ---------------------- CLI ----------------------
def _parse_bands_arg(s: str) -> Set[int]:
    out: Set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part: continue
        try: out.add(int(part))
        except ValueError: raise ValueError(f"Invalid band value: {part}")
    return out

def _parse_modes_arg(s: str) -> Set[str]:
    out: Set[str] = set()
    for part in s.split(","):
        part = part.strip().upper()
        if not part: continue
        if part not in UI_MODES:
            raise ValueError(f"Invalid mode value: {part} (valid: {', '.join(UI_MODES)})")
        out.add(part)
    return out

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="HF spot aggregator (DX Summit, optional HamAlert buffer, distance-first, time-decay, zoomable waterfall)")
    parser.add_argument("--test", action="store_true", help="Run self-tests and exit")
    parser.add_argument("--no-dxwatch", action="store_true", help="Skip scraping DXWatch (cluster)")
    parser.add_argument("--display-bands", type=str, default="", help="Comma-separated meter bands initially (e.g., 40,20,15)")
    parser.add_argument("--display-modes", type=str, default="", help=f"Comma-separated modes initially (valid: {', '.join(UI_MODES)})")
    parser.add_argument("--refresh-seconds", type=int, default=60, help="Polling interval for rows.json and backend rebuild")
    parser.add_argument("--iterations", type=int, default=1, help="Number of update cycles (0 = run forever)")
    parser.add_argument("--output", type=str, default="ssb_picks.html", help="Output HTML filename")
    args = parser.parse_args()

    if args.test or os.environ.get("RUN_TESTS") == "1":
        lat, lon = maidenhead_to_latlon("EM73ts")
        assert abs(lat-33.7708) < 0.02 and abs(lon+84.3750) < 0.02
        d = haversine_miles(33.7708, -84.3750, 40.7128, -74.0060)
        assert 700 < d < 800
        # Freshness math sanity: 45 min half-life → factor ~0.5
        f45 = time_freshness_factor(45*60); assert 0.49 < f45 < 0.51
        print("Tests passed.")
    else:
        bands = _parse_bands_arg(args.display_bands) if args.display_bands else None
        modes = _parse_modes_arg(args.display_modes) if args.display_modes else None
        out_path = Path(args.output)
        if args.iterations <= 1:
            build_and_write(out_path, no_dxwatch=args.no_dxwatch,
                            display_bands=bands, display_modes=modes,
                            refresh_seconds=args.refresh_seconds)
        else:
            serve_loop(out_path, refresh_seconds=args.refresh_seconds,
                       iterations=args.iterations, no_dxwatch=args.no_dxwatch,
                       display_bands=bands, display_modes=modes)
