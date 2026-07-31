"""Check what event types exist for repair tasks."""
import json
import sqlite3

DB = "/data/app.sqlite3"
RUN_ID = "run_c120c3aa38dd426d"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# First check what event types exist
cur = conn.execute(
    "SELECT event_type, COUNT(*) as cnt FROM event_envelopes WHERE run_id = ? GROUP BY event_type ORDER BY cnt DESC",
    (RUN_ID,),
)
print("=== Event types ===")
for r in cur.fetchall():
    print(f"  {r['event_type']:40s} {r['cnt']}")

# Check events for task_2__repair_v27 (the one that switched to Vue)
print("\n=== task_2__repair_v27 events ===")
cur = conn.execute(
    "SELECT sequence, event_type, task_id, agent_id, payload FROM event_envelopes "
    "WHERE run_id = ? AND task_id = ? ORDER BY sequence",
    (RUN_ID, "task_2__repair_v27"),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    et = r["event_type"]
    # For tool-related events, show tool name
    tool = p.get("tool", p.get("tool_name", ""))
    preview = ""
    if "preview" in p:
        preview = str(p["preview"])[:80]
    elif "path" in p:
        preview = f"path={p['path']}"
    elif "args" in p and isinstance(p["args"], dict):
        preview = f"args={str(p['args'])[:80]}"
    print(f"  seq={r['sequence']:4d} {et:30s} tool={tool:15s} {preview}")

conn.close()
