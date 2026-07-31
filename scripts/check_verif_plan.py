"""Check verification plan/criteria for tasks."""
import sqlite3
import json
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run_8dfe5fb9dae74962"
con = sqlite3.connect("/data/app.sqlite3")
con.row_factory = sqlite3.Row
cur = con.cursor()

cur.execute("SELECT task_id, payload FROM task_board_tasks WHERE run_id=?", (RUN_ID,))
for row in cur.fetchall():
    tid = row["task_id"]
    p = json.loads(row["payload"])
    md = p.get("metadata") or {}
    print(f"\n{'='*70}")
    print(f"TASK: {tid} - {p.get('title')}")
    print(f"objective: {p.get('objective')}")
    print(f"output_contract: {json.dumps(p.get('output_contract'), ensure_ascii=False, indent=2)[:500]}")

    # Check for verification plan in metadata
    vp = md.get("verification_plan") or md.get("verification") or {}
    if "verification_plan" in md:
        print(f"\nverification_plan: {json.dumps(md.get('verification_plan'), ensure_ascii=False, indent=2)[:1000]}")
    if "verification" in md:
        v = md.get("verification")
        if isinstance(v, dict):
            print(f"\nverification verdict: {v.get('verdict')}")
            print(f"verification summary: {v.get('summary')}")

    # Check all metadata keys
    print(f"\nmetadata keys: {list(md.keys())}")
    for k, v in md.items():
        if k not in ("verification", "verification_plan", "graph_version"):
            sv = str(v)
            if len(sv) > 300:
                sv = sv[:300] + "..."
            print(f"  {k}: {sv}")

con.close()
