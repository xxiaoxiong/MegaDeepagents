"""Check detailed tool call paths for repair tasks."""
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
    print(f"\n=== {tid} tool calls (read_file/create_file/edit_file only) ===")
    cur = conn.execute(
        "SELECT sequence, event_type, payload FROM event_envelopes "
        "WHERE run_id = ? AND task_id = ? AND event_type IN ('tool_call_started', 'tool_call_result') "
        "ORDER BY sequence",
        (RUN_ID, tid),
    )
    for r in cur.fetchall():
        p = json.loads(r["payload"]) if r["payload"] else {}
        tool = p.get("tool", "?")
        if tool not in ("read_file", "create_file", "edit_file", "write_file", "list_dir"):
            continue
        # Extract path from various possible payload structures
        path = ""
        if "args" in p and isinstance(p["args"], dict):
            path = p["args"].get("path", p["args"].get("file_path", p["args"].get("directory", "")))
        elif "preview" in p:
            path = str(p["preview"])[:100]
        elif "path" in p:
            path = p["path"]
        # For tool_call_result, show status
        status = p.get("status", "")
        print(f"  seq={r['sequence']:4d} {r['event_type']:20s} {tool:15s} path={path} status={status}")

conn.close()
