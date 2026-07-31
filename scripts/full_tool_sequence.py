"""Get complete tool call sequence for repair tasks."""
import json
import sqlite3

DB = "/data/app.sqlite3"
RUN_ID = "run_c120c3aa38dd426d"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

repair_tasks = [
    "task_2__repair_v27",  # switched to Vue
    "task_2__repair_v35",  # switched back to React TSX
]

for tid in repair_tasks:
    print(f"\n=== {tid} complete tool call sequence ===")
    cur = conn.execute(
        "SELECT sequence, event_type, payload FROM event_envelopes "
        "WHERE run_id = ? AND task_id = ? AND event_type = 'tool_call_started' "
        "ORDER BY sequence",
        (RUN_ID, tid),
    )
    for r in cur.fetchall():
        p = json.loads(r["payload"]) if r["payload"] else {}
        tool = p.get("tool_name", "?")
        args = p.get("arguments", {}) or {}
        path = args.get("path", args.get("file_path", args.get("directory", "")))
        print(f"  seq={r['sequence']:4d} {tool:15s} {path}")

conn.close()
