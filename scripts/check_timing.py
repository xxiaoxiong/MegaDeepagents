"""Check repair task elapsed time and recent tool calls."""
import sqlite3
import json
import sys
from datetime import datetime, UTC

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else open("/tmp/latest_run_id.txt").read().strip()

conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

# Check task_runs with timing
print("=== task_runs with timing ===")
cur = conn.execute(
    "SELECT task_id, agent_id, attempt, status, started_at, finished_at, error "
    "FROM task_runs WHERE run_id = ? ORDER BY started_at DESC LIMIT 10",
    (RUN_ID,),
)
now = datetime.now(UTC)
for r in cur.fetchall():
    d = dict(r)
    started = d.get("started_at")
    elapsed = "?"
    if started:
        try:
            st = datetime.fromisoformat(started.replace("Z", "+00:00"))
            elapsed = f"{(now - st).total_seconds():.0f}s"
        except Exception:
            pass
    print(f"  {d['task_id']:30s} {d['status']:10s} elapsed={elapsed} err={str(d['error'])[:50]}")

# Check recent tool_calls for the run
print("\n=== Recent tool_calls (last 15) ===")
try:
    cur = conn.execute(
        "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY rowid DESC LIMIT 15",
        (RUN_ID,),
    )
    cols = [d[0] for d in cur.description]
    for r in cur.fetchall():
        d = dict(r)
        # Find tool name and status fields
        tool = d.get("tool_name") or d.get("tool") or "?"
        status = d.get("status") or "?"
        task = d.get("task_id") or "?"
        ts = d.get("created_at") or d.get("timestamp") or "?"
        print(f"  {str(ts)[:19]} task={task:30s} tool={tool:20s} status={status}")
except Exception as e:
    print(f"  tool_calls query error: {e}")
    # Try tool_invocations
    try:
        cur = conn.execute(
            "SELECT * FROM tool_invocations WHERE run_id = ? ORDER BY rowid DESC LIMIT 15",
            (RUN_ID,),
        )
        cols = [d[0] for d in cur.description]
        print(f"  tool_invocations columns: {cols}")
        for r in cur.fetchall():
            d = dict(r)
            print(f"  {dict(d)}")
    except Exception as e2:
        print(f"  tool_invocations query error: {e2}")

conn.close()
