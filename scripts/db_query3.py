"""Query task board payloads and graph snapshots (JSON)."""
import sqlite3
import json
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run_8dfe5fb9dae74962"
con = sqlite3.connect("/data/app.sqlite3")
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=== task_board_tasks payloads ===")
cur.execute("SELECT task_id, payload FROM task_board_tasks WHERE run_id=?", (RUN_ID,))
for row in cur.fetchall():
    tid = row["task_id"]
    try:
        p = json.loads(row["payload"])
    except Exception:
        p = {"_raw": row["payload"]}
    print(f"\n--- {tid} ---")
    for k in ("status", "required_capabilities", "dependencies", "claimed_by", "attempts", "title", "objective", "metadata"):
        if k in p:
            v = str(p.get(k))
            if len(v) > 300:
                v = v[:300] + "..."
            print(f"  {k}: {v}")

print("\n\n=== task_graph_snapshots (latest graph) ===")
cur.execute("SELECT version, graph_json FROM task_graph_snapshots WHERE run_id=? ORDER BY version DESC LIMIT 1", (RUN_ID,))
row = cur.fetchone()
if row:
    print("version:", row["version"])
    try:
        tg = json.loads(row["graph_json"])
        if isinstance(tg, dict):
            tasks = tg.get("tasks", [])
            print(f"tasks count: {len(tasks)}")
            for t in tasks:
                tid = t.get("id") or t.get("task_id")
                print(f"\n  --- {tid} ---")
                for k in ("status", "required_capabilities", "dependencies", "claimed_by", "attempts", "title", "objective", "metadata", "output_contract"):
                    if k in t:
                        v = str(t.get(k))
                        if len(v) > 300:
                            v = v[:300] + "..."
                        print(f"    {k}: {v}")
    except Exception as e:
        print("parse error:", e)
        print("graph_json (first 500):", row["graph_json"][:500])

con.close()
