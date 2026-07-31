"""Quick check of task capabilities and contracts for a run."""
import sqlite3
import json
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else open("/tmp/latest_run_id.txt").read().strip()

conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? ORDER BY updated_at",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    caps = p.get("required_capabilities", [])
    oc = p.get("output_contract", {}) or {}
    ac = oc.get("acceptance_criteria", []) or []
    st = p.get("status", "?")
    agent = p.get("assigned_agent_id") or "-"
    budget = p.get("budget", {}) or {}
    max_sec = budget.get("max_seconds", "?")
    print(f"{r['task_id']:30s} status={st:14s} agent={agent} caps={caps} timeout={max_sec}s criteria={len(ac)}")
    if ac:
        for c in ac[:4]:
            print(f"    - {str(c)[:90]}")
conn.close()
