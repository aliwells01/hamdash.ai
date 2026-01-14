#!/usr/bin/env python3
import os
import time
import json
import sqlite3
import subprocess
from typing import Any, Dict, List, Optional

from aiohttp import web

DB_DEFAULT = "/Users/aw/documents/hobbies/ham/radio_intel/data/spots.sqlite"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_LIVE_PATH = os.path.join(BASE_DIR, "run_live.py")

run_live_proc: Optional[subprocess.Popen] = None


def _db_path(req: web.Request) -> str:
    return req.query.get("db") or DB_DEFAULT


def _table_cols(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def _safe_select_recent_edges(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    # Try to read common columns from pota_heard_edges, but don’t fail if schema differs.
    cols = set(_table_cols(conn, "pota_heard_edges"))
    wanted = [
        ("spot_time_utc", "time_utc"),
        ("activator_call", "activator"),
        ("park_ref", "park"),
        ("spotter_call", "spotter"),
        ("band", "band"),
        ("mode", "mode"),
        ("frequency_hz", "freq_hz"),
        ("dist_km", "dist_km"),
        ("dist_mi", "dist_mi"),
        ("score", "score"),
    ]
    select_exprs = []
    for col, alias in wanted:
        if col in cols:
            select_exprs.append(f"{col} AS {alias}")

    # If nothing matches, return empty.
    if not select_exprs:
        return []

    sql = f"""
      SELECT {", ".join(select_exprs)}
      FROM pota_heard_edges
      ORDER BY spot_time_utc DESC
      LIMIT ?
    """
    cur = conn.execute(sql, (limit,))
    rows = cur.fetchall()
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in rows]


def _safe_select_park_status(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    # Same idea: adapt to whatever columns exist in pota_park_status_now
    cols = set(_table_cols(conn, "pota_park_status_now"))
    wanted = [
        ("updated_utc", "updated_utc"),
        ("activator_call", "activator"),
        ("park_ref", "park"),
        ("edges", "edges"),
        ("uniq_states", "uniq_states"),
        ("score", "score"),
        ("last_heard_utc", "last_heard_utc"),
        ("last_freq_hz", "last_freq_hz"),
        ("last_mode", "last_mode"),
        ("last_band", "last_band"),
    ]
    select_exprs = []
    for col, alias in wanted:
        if col in cols:
            select_exprs.append(f"{col} AS {alias}")

    if not select_exprs:
        return []

    order_col = "last_heard_utc" if "last_heard_utc" in cols else ("score" if "score" in cols else "updated_utc")
    sql = f"""
      SELECT {", ".join(select_exprs)}
      FROM pota_park_status_now
      ORDER BY {order_col} DESC
      LIMIT ?
    """
    cur = conn.execute(sql, (limit,))
    rows = cur.fetchall()
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in rows]


def _run_step(step: str, db: str) -> Dict[str, Any]:
    # Adjust these command names if yours differ.
    cmds = {
        "ssb_agent": ["python3", os.path.join(BASE_DIR, "ssb_agent.py")],
        "pota_refresh": ["python3", os.path.join(BASE_DIR, "run_pota_refresh.py"), "--db", db],
        "qrz_enrich": ["python3", os.path.join(BASE_DIR, "qrz_enrich_spotters.py"), "--db", db, "--limit", "400"],
        "build_edges_recent": ["python3", os.path.join(BASE_DIR, "build_pota_edges.py"), "--db", db, "--minutes", "60", "--limit", "50000"],
        "make_status": ["python3", os.path.join(BASE_DIR, "make_park_status.py"), "--window-min", "30", "--min-edges", "1"],
    }
    if step not in cmds:
        return {"ok": False, "error": f"Unknown step '{step}'"}

    t0 = time.time()
    p = subprocess.run(cmds[step], capture_output=True, text=True)
    return {
        "ok": p.returncode == 0,
        "step": step,
        "rc": p.returncode,
        "elapsed_s": round(time.time() - t0, 2),
        "stdout": p.stdout[-6000:],  # keep last chunk
        "stderr": p.stderr[-6000:],
        "cmd": cmds[step],
    }


routes = web.RouteTableDef()


@routes.get("/")
async def root(req: web.Request):
    # Serve the dashboard HTML
    return web.FileResponse(os.path.join(BASE_DIR, "pota-scoring.html"))


@routes.post("/api/run_live/start")
async def start_live(req: web.Request):
    global run_live_proc
    if run_live_proc and run_live_proc.poll() is None:
        return web.json_response({"ok": True, "running": True, "pid": run_live_proc.pid})

    if not os.path.exists(RUN_LIVE_PATH):
        return web.json_response({"ok": False, "error": f"run_live.py not found at {RUN_LIVE_PATH}"}, status=400)

    # start as a background process
    run_live_proc = subprocess.Popen(["python3", RUN_LIVE_PATH], cwd=BASE_DIR)
    return web.json_response({"ok": True, "running": True, "pid": run_live_proc.pid})


@routes.post("/api/run_live/stop")
async def stop_live(req: web.Request):
    global run_live_proc
    if not run_live_proc or run_live_proc.poll() is not None:
        return web.json_response({"ok": True, "running": False})

    run_live_proc.terminate()
    try:
        run_live_proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        run_live_proc.kill()
    return web.json_response({"ok": True, "running": False})


@routes.get("/api/run_live/status")
async def live_status(req: web.Request):
    global run_live_proc
    running = bool(run_live_proc and run_live_proc.poll() is None)
    pid = run_live_proc.pid if running else None
    return web.json_response({"ok": True, "running": running, "pid": pid})


@routes.post("/api/step/{step}")
async def run_step(req: web.Request):
    step = req.match_info["step"]
    db = _db_path(req)
    res = _run_step(step, db)
    status = 200 if res.get("ok") else 400
    return web.json_response(res, status=status)


@routes.get("/api/data")
async def get_data(req: web.Request):
    db = _db_path(req)
    limit = int(req.query.get("limit", "50"))

    if not os.path.exists(db):
        return web.json_response({"ok": False, "error": f"DB not found: {db}"}, status=400)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    out: Dict[str, Any] = {"ok": True, "db": db}

    # Basic counts
    def count(table: str) -> int:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            return -1

    out["counts"] = {
        "pota_spot_history": count("pota_spot_history"),
        "pota_heard_edges": count("pota_heard_edges"),
        "callsign_location": count("callsign_location"),
        "pota_park_status_now": count("pota_park_status_now"),
    }

    # Tables
    try:
        out["recent_edges"] = _safe_select_recent_edges(conn, limit=limit)
    except Exception as e:
        out["recent_edges_error"] = str(e)
        out["recent_edges"] = []

    try:
        out["park_status_now"] = _safe_select_park_status(conn, limit=limit)
    except Exception as e:
        out["park_status_now_error"] = str(e)
        out["park_status_now"] = []

    conn.close()
    return web.json_response(out)


def main():
    app = web.Application()
    app.add_routes(routes)
    web.run_app(app, host="127.0.0.1", port=8787)


if __name__ == "__main__":
    main()
