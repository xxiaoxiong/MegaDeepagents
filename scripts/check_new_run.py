"""Quick check of a run's state. Usage: python check_new_run.py <run_id>"""
import json
import sqlite3
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run_c120c3aa38dd426d"
DB = "/data/app.sqlite3"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

cur = conn.execute("SELECT status, updated_at FROM team_runs WHERE run_id = ?", (RUN_ID,))
r = cur.fetchone()
print(f"=== Run {RUN_ID} ===")
print(f"status={r['status'] if r else 'not found'} updated_at={r['updated_at'] if r else '-'}")

print("\n=== Tasks ===")
cur = conn.execute(
    "SELECT task_id, payload, updated_at FROM task_board_tasks WHERE run_id = ? ORDER BY updated_at",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    st = p.get("status", "?")
    caps = p.get("required_capabilities", [])
    meta = p.get("metadata", {}) or {}
    verif = meta.get("verification", {}) if isinstance(meta, dict) else {}
    v = verif.get("verdict", "-") if verif else "-"
    fc = verif.get("failed_criteria", []) if verif else []
    fc_s = ""
    if isinstance(fc, list) and fc:
        first = fc[0]
        if isinstance(first, dict):
            fc_s = first.get("criterion", "?")
        else:
            fc_s = str(first)[:40]
    produced = p.get("produced_artifact_ids", []) or []
    superseded = bool(meta.get("superseded_by_repair")) if isinstance(meta, dict) else False
    sup_marker = " [superseded]" if superseded else ""
    print(f"  {r['task_id']:35s} {st:18s} v={v:10s} fail={fc_s:20s} arts={len(produced)}{sup_marker}")

print("\n=== Recent events (last 10) ===")
cur = conn.execute(
    "SELECT sequence, event_type, task_id, agent_id FROM event_envelopes WHERE run_id = ? ORDER BY sequence DESC LIMIT 10",
    (RUN_ID,),
)
for r in cur.fetchall():
    d = dict(r)
    print(f"  seq={d['sequence']:4d} {d['event_type']:35s} task={d['task_id'] or '-':30s} agent={d['agent_id'] or '-'}")

conn.close()
