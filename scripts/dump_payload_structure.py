"""Dump raw payload structure of tool events."""
import json
import sqlite3

DB = "/data/app.sqlite3"
RUN_ID = "run_c120c3aa38dd426d"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Get a few sample tool events for task_2__repair_v27
cur = conn.execute(
    "SELECT sequence, event_type, payload FROM event_envelopes "
    "WHERE run_id = ? AND task_id = ? AND event_type IN ('tool_call_started', 'tool_call_result', 'BeforeToolUse', 'AfterToolUse') "
    "ORDER BY sequence LIMIT 10",
    (RUN_ID, "task_2__repair_v27"),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    print(f"seq={r['sequence']} {r['event_type']}")
    print(f"  payload keys: {list(p.keys())}")
    print(f"  payload: {json.dumps(p, ensure_ascii=False)[:300]}")
    print()

conn.close()
