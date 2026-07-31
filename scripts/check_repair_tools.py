"""Check what tools the repair agent called - did it read source artifacts?"""
import json
import sqlite3

DB = "/data/app.sqlite3"
RUN_ID = "run_c120c3aa38dd426d"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Check tool calls for each task_2 repair task
repair_tasks = [
    "task_2__repair_v11",
    "task_2__repair_v19",
    "task_2__repair_v27",
    "task_2__repair_v31",
    "task_2__repair_v35",
]

for tid in repair_tasks:
    print(f"\n=== {tid} tool calls ===")
    cur = conn.execute(
        "SELECT sequence, event_type, task_id, agent_id, payload FROM event_envelopes "
        "WHERE run_id = ? AND task_id = ? AND event_type = 'tool_call' "
        "ORDER BY sequence",
        (RUN_ID, tid),
    )
    rows = cur.fetchall()
    for r in rows:
        p = json.loads(r["payload"]) if r["payload"] else {}
        tool = p.get("tool", "?")
        args = p.get("args", {}) or {}
        # For read_file, show the path
        if tool in ("read_file", "read"):
            path = args.get("path", args.get("file_path", "?"))
            print(f"  seq={r['sequence']:4d} {tool:20s} path={path}")
        elif tool in ("create_file", "write_file", "edit_file", "create", "edit"):
            path = args.get("path", args.get("file_path", "?"))
            print(f"  seq={r['sequence']:4d} {tool:20s} path={path}")
        else:
            print(f"  seq={r['sequence']:4d} {tool}")

conn.close()
