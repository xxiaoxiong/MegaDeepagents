"""Dump full verification metadata for repair tasks."""
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
    verif = md.get("verification")
    if verif:
        print(f"\n{'='*70}")
        print(f"TASK: {tid}")
        print(f"verdict: {verif.get('verdict')}")
        print(f"summary: {verif.get('summary')}")
        print(f"\nfailed_criteria:")
        for fc in verif.get("failed_criteria", []):
            print(f"  - criterion: {fc.get('criterion')}")
            print(f"    severity: {fc.get('severity')}")
            print(f"    detail: {fc.get('detail')}")
            print(f"    proposed_fix: {fc.get('proposed_fix')}")
            print(f"    affected_files: {fc.get('affected_files')}")
        print(f"\npassed_criteria: {verif.get('passed_criteria')}")
        print(f"\nproposed_tasks: {json.dumps(verif.get('proposed_tasks'), ensure_ascii=False, indent=2)[:500]}")
        ev = verif.get("evidence")
        if ev:
            print(f"\nevidence: {json.dumps(ev, ensure_ascii=False)[:500]}")

con.close()
