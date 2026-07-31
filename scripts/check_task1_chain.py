"""Check repair artifacts and verification feedback for task_1 chain."""
import sqlite3
import json

RUN_ID = "run_55507ebfce5744e8"
conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

# 1. Artifacts for task_1 chain
print("=== task_1 chain artifacts ===")
cur = conn.execute(
    "SELECT artifact_id, task_id, relative_path, size_bytes, status FROM artifacts WHERE run_id = ? AND (task_id LIKE 'task_1%') ORDER BY task_id, artifact_id",
    (RUN_ID,),
)
for r in cur.fetchall():
    print(f"  {r['task_id']:35s} {r['relative_path']:55s} size={r['size_bytes']:6d} status={r['status']}")

# 2. Verification details for task_1 chain
print("\n=== task_1 chain verification ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND (task_id LIKE 'task_1%') ORDER BY task_id",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    meta = p.get("metadata", {}) or {}
    verif = meta.get("verification", {}) if isinstance(meta, dict) else {}
    st = p.get("status", "?")
    if verif:
        v = verif.get("verdict", "?")
        fc = verif.get("failed_criteria", [])
        summary = verif.get("summary", "")
        print(f"\n  {r['task_id']} ({st}, v={v})")
        print(f"    summary: {summary[:200]}")
        if isinstance(fc, list):
            for c in fc[:3]:
                if isinstance(c, dict):
                    print(f"    FAIL: {c.get('criterion','?')} -> {str(c.get('detail',''))[:200]}")
    # Check verification_feedback (what this task received as input)
    vf = meta.get("verification_feedback", {}) if isinstance(meta, dict) else {}
    if vf:
        print(f"    [received feedback] verdict={vf.get('verdict')}")
        vfc = vf.get("failed_criteria", [])
        if isinstance(vfc, list):
            for c in vfc[:2]:
                if isinstance(c, dict):
                    print(f"    feedback: {c.get('criterion','?')} -> {str(c.get('detail',''))[:150]}")

# 3. task_1 output contract
print("\n=== task_1 output contract ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id = 'task_1'",
    (RUN_ID,),
)
r = cur.fetchone()
if r:
    p = json.loads(r["payload"]) if r["payload"] else {}
    print(f"  objective: {p.get('objective','')}")
    print(f"  description: {p.get('description','')[:200]}")

conn.close()
