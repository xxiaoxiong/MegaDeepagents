"""Check task objectives and the garbled run status."""
import json
import sqlite3

DB = "/data/app.sqlite3"
RUN_ID = "run_4052c79250a0403c"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("=== Task objectives ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? ORDER BY task_id",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    tid = r["task_id"]
    obj = p.get("objective", "")[:80]
    caps = p.get("required_capabilities", [])
    st = p.get("status", "?")
    print(f"  {tid:20s} status={st:12s} caps={caps} obj={obj}")

# Check the garbled run status
print("\n=== Garbled run status ===")
cur = conn.execute(
    "SELECT run_id, status, goal FROM team_runs WHERE run_id = ?",
    ("run_de866d4e976b4c3a",),
)
r = cur.fetchone()
if r:
    print(f"  {r['run_id']}: status={r['status']} goal={r['goal'][:30]}")

# Check events for the garbled run
cur = conn.execute(
    "SELECT event_type, COUNT(*) as cnt FROM event_envelopes WHERE run_id = ? GROUP BY event_type ORDER BY cnt DESC LIMIT 5",
    ("run_de866d4e976b4c3a",),
)
print("  Garbled run events:")
for r in cur.fetchall():
    print(f"    {r['event_type']}: {r['cnt']}")

conn.close()
