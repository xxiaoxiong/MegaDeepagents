"""Check repair task objective for compounding fix verification."""
import sqlite3
import json

RUN_ID = "run_55507ebfce5744e8"
conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

print("=== All tasks and their objectives ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? ORDER BY updated_at",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    st = p.get("status", "?")
    obj = p.get("objective", "")
    print(f"  {r['task_id']:35s} {st:18s} objective={obj[:100]}")

conn.close()
