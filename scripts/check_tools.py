"""Check recent tool invocations to confirm agent activity."""
import sqlite3
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else open("/tmp/latest_run_id.txt").read().strip()

conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

cur = conn.execute(
    "SELECT task_id, tool_name, status, created_at FROM tool_invocations "
    "WHERE run_id = ? ORDER BY rowid DESC LIMIT 20",
    (RUN_ID,),
)
rows = cur.fetchall()
print(f"Recent tool_invocations ({len(rows)}):")
for r in rows:
    d = dict(r)
    print(f"  {str(d['created_at'])[:19]} task={d['task_id']:30s} tool={d['tool_name']:20s} status={d['status']}")

# Count by status
cur = conn.execute(
    "SELECT status, COUNT(*) as n FROM tool_invocations WHERE run_id = ? GROUP BY status",
    (RUN_ID,),
)
print("\nTool invocation status summary:")
for r in cur.fetchall():
    print(f"  {r['status']}: {r['n']}")

conn.close()
