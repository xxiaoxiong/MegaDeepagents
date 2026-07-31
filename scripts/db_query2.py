"""Query task board and verification storage."""
import sqlite3
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run_8dfe5fb9dae74962"
con = sqlite3.connect("/data/app.sqlite3")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=== task_board_tasks schema ===")
cur.execute("PRAGMA table_info(task_board_tasks)")
cols = [r[1] for r in cur.fetchall()]
print("COLUMNS:", cols)

print("\n=== task_board_tasks for run ===")
cur.execute("SELECT * FROM task_board_tasks WHERE run_id=?", (RUN_ID,))
for row in cur.fetchall():
    d = dict(row)
    tid = d.get("task_id", "")
    print(f"\n--- {tid} ---")
    for k in ("status", "required_capabilities", "dependencies", "claimed_by", "attempts", "title", "objective"):
        if k in d:
            v = str(d.get(k))
            if len(v) > 200:
                v = v[:200] + "..."
            print(f"  {k}: {v}")
    # metadata
    md = d.get("metadata")
    if md:
        print(f"  metadata: {str(md)[:300]}")

print("\n=== task_graph_snapshots schema ===")
cur.execute("PRAGMA table_info(task_graph_snapshots)")
cols = [r[1] for r in cur.fetchall()]
print("COLUMNS:", cols)

print("\n=== task_graph_snapshots for run (latest) ===")
cur.execute("SELECT * FROM task_graph_snapshots WHERE run_id=? ORDER BY version DESC LIMIT 1", (RUN_ID,))
row = cur.fetchone()
if row:
    d = dict(row)
    print("version:", d.get("version"))
    import json
    try:
        tg = json.loads(d.get("graph_json") or d.get("task_graph_json") or "{}")
        for t in tg.get("tasks", []) if isinstance(tg, dict) else []:
            print(f"  task {t.get('id')}: caps={t.get('required_capabilities')} deps={t.get('dependencies')} status={t.get('status')}")
    except Exception as e:
        print("parse error:", e)
        print("graph_json keys:", list(d.keys()))

con.close()
