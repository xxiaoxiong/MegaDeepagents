"""Check recent event envelopes for agent activity."""
import sqlite3
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else open("/tmp/latest_run_id.txt").read().strip()

conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

cur = conn.execute(
    "SELECT sequence, event_type, task_id, agent_id, timestamp "
    "FROM event_envelopes WHERE run_id = ? ORDER BY sequence DESC LIMIT 25",
    (RUN_ID,),
)
rows = cur.fetchall()
print(f"Recent events ({len(rows)}):")
for r in rows:
    d = dict(r)
    print(f"  seq={d['sequence']:>5} {str(d['timestamp'])[:19]} {d['event_type']:30s} "
          f"task={d['task_id'] or '-':30s} agent={d['agent_id'] or '-'}")

conn.close()
