"""Check v15 artifacts."""
import sqlite3
import json

RUN_ID = "run_55507ebfce5744e8"
conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

print("=== task_1__repair_v15 artifacts ===")
cur = conn.execute(
    "SELECT artifact_id, task_id, relative_path, size_bytes, status FROM artifacts WHERE run_id = ? AND task_id = 'task_1__repair_v15'",
    (RUN_ID,),
)
for r in cur.fetchall():
    print(f"  {r['relative_path']:55s} size={r['size_bytes']:6d} status={r['status']}")

print("\n=== v15 task details ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id = 'task_1__repair_v15'",
    (RUN_ID,),
)
r = cur.fetchone()
if r:
    p = json.loads(r["payload"]) if r["payload"] else {}
    print(f"  status: {p.get('status')}")
    meta = p.get("metadata", {}) or {}
    vf = meta.get("verification_feedback", {}) if isinstance(meta, dict) else {}
    if vf:
        print(f"  received feedback verdict: {vf.get('verdict')}")
        vfc = vf.get("failed_criteria", [])
        if isinstance(vfc, list):
            for c in vfc[:3]:
                if isinstance(c, dict):
                    print(f"    feedback: {c.get('criterion','?')} -> {str(c.get('detail',''))[:200]}")

conn.close()
