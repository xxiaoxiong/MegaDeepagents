"""Check verification feedback for task_2 and task_3 repair chains."""
import sqlite3
import json

RUN_ID = "run_55507ebfce5744e8"
conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

# Check verification details for task_2 and task_3 chains
for task_prefix in ["task_2", "task_3"]:
    print(f"\n=== {task_prefix} chain verification ===")
    cur = conn.execute(
        "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id LIKE ? ORDER BY task_id",
        (RUN_ID, f"{task_prefix}%"),
    )
    for r in cur.fetchall():
        p = json.loads(r["payload"]) if r["payload"] else {}
        st = p.get("status", "?")
        meta = p.get("metadata", {}) or {}
        verif = meta.get("verification", {}) if isinstance(meta, dict) else {}
        vf = meta.get("verification_feedback", {}) if isinstance(meta, dict) else {}
        
        print(f"\n  {r['task_id']} ({st})")
        if verif:
            v = verif.get("verdict", "?")
            fc = verif.get("failed_criteria", [])
            summary = verif.get("summary", "")
            print(f"    verdict: {v}")
            print(f"    summary: {summary[:200]}")
            if isinstance(fc, list):
                for c in fc[:3]:
                    if isinstance(c, dict):
                        print(f"    FAIL: {c.get('criterion','?')} -> {str(c.get('detail',''))[:200]}")
        
        if vf:
            print(f"    [received feedback] verdict={vf.get('verdict')}")
            vfc = vf.get("failed_criteria", [])
            if isinstance(vfc, list):
                for c in vfc[:2]:
                    if isinstance(c, dict):
                        print(f"    feedback: {c.get('criterion','?')} -> {str(c.get('detail',''))[:200]}")

# Check current running task
print("\n=== Current running tasks ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id IN ('task_2__repair_v31', 'task_3__repair_v33') ORDER BY task_id",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    print(f"  {r['task_id']}: status={p.get('status')}")
    meta = p.get("metadata", {}) or {}
    vf = meta.get("verification_feedback", {}) if isinstance(meta, dict) else {}
    if vf:
        vfc = vf.get("failed_criteria", [])
        if isinstance(vfc, list):
            for c in vfc[:3]:
                if isinstance(c, dict):
                    print(f"    feedback: {c.get('criterion','?')} -> {str(c.get('detail',''))[:200]}")

conn.close()
