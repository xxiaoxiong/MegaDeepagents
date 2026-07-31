"""Check recent task_runs activity and verification results for a run."""
import sqlite3
import json
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else open("/tmp/latest_run_id.txt").read().strip()

conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

# Recent task_runs
print("=== Recent task_runs ===")
cur = conn.execute(
    "SELECT task_id, agent_id, attempt, status, started_at, finished_at, error "
    "FROM task_runs WHERE run_id = ? ORDER BY started_at DESC LIMIT 15",
    (RUN_ID,),
)
for r in cur.fetchall():
    d = dict(r)
    print(f"  {d['task_id']:30s} att={d['attempt']} {d['status']:10s} "
          f"agent={d['agent_id']} err={str(d['error'])[:60]}")

# Verification details from task_board metadata
print("\n=== Verification details ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? ORDER BY updated_at",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    meta = p.get("metadata", {}) or {}
    verif = meta.get("verification", {}) if isinstance(meta, dict) else {}
    st = p.get("status", "?")
    tid = r["task_id"]
    if verif:
        v = verif.get("verdict", "?")
        fc = verif.get("failed_criteria", [])
        print(f"  {tid:30s} {st:16s} v={v}")
        if isinstance(fc, list):
            for c in fc[:3]:
                if isinstance(c, dict):
                    print(f"    fail: {c.get('criterion','?')} -> {str(c.get('detail',''))[:120]}")
                else:
                    print(f"    fail: {str(c)[:120]}")
    else:
        print(f"  {tid:30s} {st:16s} (no verification yet)")

# Check if the run is still active
cur = conn.execute("SELECT status, updated_at FROM team_runs WHERE run_id = ?", (RUN_ID,))
row = cur.fetchone()
if row:
    print(f"\nRun status: {row[0]} updated_at={row[1]}")

conn.close()
