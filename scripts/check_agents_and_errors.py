"""Check agent spawning and file operation failures in recent runs."""
import json
import sqlite3

DB = "/data/app.sqlite3"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Check the most recent meaningful run
RUN_ID = "run_de866d4e976b4c3a"

print("=== Agent spawning ===")
cur = conn.execute(
    "SELECT sequence, event_type, agent_id, payload FROM event_envelopes "
    "WHERE run_id = ? AND event_type = 'agent_spawned' ORDER BY sequence",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    print(f"  seq={r['sequence']} agent={r['agent_id']} profile={p.get('profile_id', '?')} session={p.get('session_id', '?')}")

print("\n=== File operation failures ===")
cur = conn.execute(
    "SELECT sequence, event_type, task_id, agent_id, payload FROM event_envelopes "
    "WHERE run_id = ? AND event_type = 'tool_call_result' "
    "AND payload LIKE '%error%' ORDER BY sequence LIMIT 20",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    tool = p.get("tool_name", "?")
    status = p.get("status", "?")
    preview = p.get("result_preview", "")[:150]
    print(f"  seq={r['sequence']} task={r['task_id']} tool={tool} status={status} preview={preview}")

# Also check BeforeToolUse events with errors
print("\n=== Tool errors (BeforeToolUse/AfterToolUse) ===")
cur = conn.execute(
    "SELECT sequence, event_type, task_id, payload FROM event_envelopes "
    "WHERE run_id = ? AND event_type IN ('BeforeToolUse', 'AfterToolUse') "
    "AND payload LIKE '%error%' ORDER BY sequence LIMIT 15",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    tool = p.get("tool", "?")
    result = p.get("result", {})
    error = result.get("error", "") if isinstance(result, dict) else str(result)[:150]
    print(f"  seq={r['sequence']} {r['event_type']} task={r['task_id']} tool={tool} error={error[:150]}")

# Check which agents worked on which tasks
print("\n=== Task-agent assignment ===")
cur = conn.execute(
    "SELECT DISTINCT task_id, agent_id FROM event_envelopes "
    "WHERE run_id = ? AND event_type = 'TaskStarted' ORDER BY task_id",
    (RUN_ID,),
)
for r in cur.fetchall():
    print(f"  task={r['task_id']:35s} agent={r['agent_id']}")

conn.close()
