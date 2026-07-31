"""Check planning failure details."""
import json
import sqlite3

DB = "/data/app.sqlite3"
RUN_ID = "run_3ccd86eb07ac4cf2"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

cur = conn.execute(
    "SELECT sequence, event_type, payload FROM event_envelopes "
    "WHERE run_id = ? ORDER BY sequence",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    et = r["event_type"]
    if "error" in p or "detail" in p or "reason" in p:
        print(f"seq={r['sequence']} {et}")
        print(f"  payload: {json.dumps(p, ensure_ascii=False)[:500]}")
    else:
        print(f"seq={r['sequence']} {et} keys={list(p.keys())[:5]}")

conn.close()
