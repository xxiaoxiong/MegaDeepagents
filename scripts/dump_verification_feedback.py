"""Dump detailed verification feedback for a task's repair chain."""
import json
import sqlite3
import sys

DB = "/data/app.sqlite3"
RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run_c120c3aa38dd426d"
TASK_PREFIX = sys.argv[2] if len(sys.argv) > 2 else "task_2"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

cur = conn.execute(
    "SELECT task_id, payload, updated_at FROM task_board_tasks WHERE run_id = ? ORDER BY updated_at",
    (RUN_ID,),
)

print(f"=== Verification feedback for tasks matching '{TASK_PREFIX}' in {RUN_ID} ===\n")
for r in cur.fetchall():
    tid = r["task_id"]
    if not tid.startswith(TASK_PREFIX):
        continue
    p = json.loads(r["payload"]) if r["payload"] else {}
    st = p.get("status", "?")
    meta = p.get("metadata", {}) or {}
    verif = meta.get("verification", {}) if isinstance(meta, dict) else {}
    v = verif.get("verdict", "-") if verif else "-"
    summary = verif.get("summary", "") if verif else ""
    fc = verif.get("failed_criteria", []) if verif else []
    scores = verif.get("scores", {}) if verif else {}
    objective = p.get("objective", "")[:100]

    print(f"--- {tid} ---")
    print(f"  status: {st}")
    print(f"  verdict: {v}")
    print(f"  summary: {summary}")
    print(f"  scores: {scores}")
    print(f"  objective: {objective}")
    if fc:
        print(f"  failed_criteria ({len(fc)}):")
        for i, c in enumerate(fc):
            if isinstance(c, dict):
                crit = c.get("criterion", "?")
                detail = (c.get("detail", "") or "")[:200]
                sev = c.get("severity", "?")
                print(f"    [{i}] {crit} (severity={sev})")
                print(f"        detail: {detail}")
            else:
                print(f"    [{i}] {c}")
    print()

conn.close()
