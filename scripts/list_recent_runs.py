"""List recent runs and their task states. No arguments needed."""
import json
import sqlite3

DB = "/data/app.sqlite3"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("=== Recent runs (last 5) ===")
cur = conn.execute(
    "SELECT run_id, status, goal, updated_at FROM team_runs ORDER BY updated_at DESC LIMIT 5"
)
runs = cur.fetchall()
for r in runs:
    goal = (r["goal"] or "")[:50]
    print(f"  {r['run_id']:40s} {r['status']:12s} {r['updated_at']}  {goal}")

if not runs:
    print("  (no runs found)")
    conn.close()
    raise SystemExit(0)

# Show details of the most recent run
latest = runs[0]
run_id = latest["run_id"]
print(f"\n=== Latest run details: {run_id} ===")
print(f"status={latest['status']} updated_at={latest['updated_at']}")

print("\n=== Tasks ===")
cur = conn.execute(
    "SELECT task_id, payload, updated_at FROM task_board_tasks WHERE run_id = ? ORDER BY updated_at",
    (run_id,),
)
tasks = cur.fetchall()
for r in tasks:
    p = json.loads(r["payload"]) if r["payload"] else {}
    st = p.get("status", "?")
    meta = p.get("metadata", {}) or {}
    verif = meta.get("verification", {}) if isinstance(meta, dict) else {}
    v = verif.get("verdict", "-") if verif else "-"
    fc = verif.get("failed_criteria", []) if verif else []
    fc_s = ""
    if isinstance(fc, list) and fc:
        first = fc[0]
        if isinstance(first, dict):
            fc_s = (first.get("criterion", "?") or "")[:30]
        else:
            fc_s = str(first)[:30]
    produced = p.get("produced_artifact_ids", []) or []
    superseded = bool(meta.get("superseded_by_repair")) if isinstance(meta, dict) else False
    sup_marker = " [superseded]" if superseded else ""
    print(f"  {r['task_id']:35s} {st:18s} v={v:10s} fail={fc_s:25s} arts={len(produced)}{sup_marker}")

print("\n=== Recent events (last 15) ===")
cur = conn.execute(
    "SELECT sequence, event_type, task_id, agent_id FROM event_envelopes WHERE run_id = ? ORDER BY sequence DESC LIMIT 15",
    (run_id,),
)
for r in cur.fetchall():
    d = dict(r)
    print(f"  seq={d['sequence']:4d} {d['event_type']:35s} task={d['task_id'] or '-':30s} agent={d['agent_id'] or '-'}")

# Count repair tasks per base task
print("\n=== Repair chain summary ===")
from collections import Counter
repair_counter = Counter()
for r in tasks:
    tid = r["task_id"]
    if "__repair_v" in tid:
        base = tid.split("__repair_v")[0]
        repair_counter[base] += 1
if repair_counter:
    for base, count in sorted(repair_counter.items()):
        print(f"  {base}: {count} repair task(s)")
else:
    print("  (no repair tasks)")

conn.close()
