"""Check current state of run_e705290b97cf4a14."""
import sqlite3
import json

RUN_ID = "run_55507ebfce5744e8"
conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

# Run status
cur = conn.execute("SELECT status, updated_at FROM team_runs WHERE run_id = ?", (RUN_ID,))
r = cur.fetchone()
print(f"=== Run {RUN_ID} ===")
print(f"status={r['status'] if r else 'not found'} updated_at={r['updated_at'] if r else '-'}")

# Task statuses
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
    print(f"  {r['task_id']:35s} {st:18s} v={v:10s} fail={fc_s:20s} arts={len(produced)} caps={caps}")

# Recent task_runs (last 10)
print("\n=== Recent task_runs (last 10) ===")
cur = conn.execute(
    "SELECT task_id, agent_id, attempt, status, started_at, finished_at, error "
    "FROM task_runs WHERE run_id = ? ORDER BY started_at DESC LIMIT 10",
    (RUN_ID,),
)
for r in cur.fetchall():
    d = dict(r)
    err = (d["error"] or "")[:80]
    print(f"  {d['task_id']:35s} att={d['attempt']} {d['status']:10s} agent={d['agent_id']} err={err}")

# Recent events (last 15)
print("\n=== Recent events (last 15) ===")
cur = conn.execute(
    "SELECT sequence, event_type, task_id, agent_id, timestamp "
    "FROM event_envelopes WHERE run_id = ? ORDER BY sequence DESC LIMIT 15",
    (RUN_ID,),
)
for r in cur.fetchall():
    d = dict(r)
    print(f"  seq={d['sequence']:4d} {d['event_type']:35s} task={d['task_id'] or '-':30s} agent={d['agent_id'] or '-'}")

# Check max_repair_rounds config
print("\n=== Graph snapshot (repair round info) ===")
cur = conn.execute(
    "SELECT payload FROM task_graph_snapshots WHERE run_id = ? ORDER BY version DESC LIMIT 1",
    (RUN_ID,),
)
r = cur.fetchone()
if r:
    g = json.loads(r["payload"])
    print(f"  version={g.get('version')} max_repair_rounds={g.get('max_repair_rounds', 'n/a')}")
    nodes = g.get("nodes", {})
    repair_count = sum(1 for n in nodes.values() if "__repair" in n.get("id", ""))
    print(f"  total_nodes={len(nodes)} repair_nodes={repair_count}")

conn.close()
