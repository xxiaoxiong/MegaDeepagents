"""Dump raw payload of one task to understand the stored structure."""
import sqlite3
import json
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else open("/tmp/latest_run_id.txt").read().strip()
TASK_ID = sys.argv[2] if len(sys.argv) > 2 else "task_2"

conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id = ?",
    (RUN_ID, TASK_ID),
)
row = cur.fetchone()
if row:
    p = json.loads(row["payload"]) if row["payload"] else {}
    # Print all keys
    print("Keys:", list(p.keys()))
    print("output_contract:", json.dumps(p.get("output_contract"), ensure_ascii=False, indent=2))
    print("budget:", json.dumps(p.get("budget"), ensure_ascii=False, default=str))
    print("required_capabilities:", p.get("required_capabilities"))
    print("max_attempts:", p.get("max_attempts"))
conn.close()
