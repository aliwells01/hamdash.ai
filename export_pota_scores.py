import json, sqlite3, pathlib

DB_PATH = pathlib.Path("data/spots.sqlite")
OUT_PATH = pathlib.Path("web/pota_scores_now.json")

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT park, score
        FROM pota_park_status_now
        WHERE score IS NOT NULL
    """)
    rows = cur.fetchall()
    conn.close()

    data = {r["park"]: r["score"] for r in rows}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))
    print(f"Wrote {OUT_PATH} with {len(data)} parks")

if __name__ == "__main__":
    main()
