"""Check task_3__repair_v31 progress and feedback."""
import sqlite3
import json

RUN_ID = "run_e705290b97cf4a14"
conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

# 1. v31 task details
print("=== task_3__repair_v31 details ===")
cur = conn.execute(
    "SELECT task_id, payload, updated_at FROM task_board_tasks WHERE run_id = ? AND task_id = 'task_3__repair_v31'",
    (RUN_ID,),
)
r = cur.fetchone()
if r:
    p = json.loads(r["payload"]) if r["payload"] else {}
    print(f"  status: {p.get('status')}")
    print(f"  objective: {p.get('objective','')[:300]}")
    print(f"  description: {p.get('description','')[:500]}")
    meta = p.get("metadata", {}) or {}
    print(f"  metadata keys: {list(meta.keys()) if isinstance(meta, dict) else 'n/a'}")
    repair_feedback = meta.get("repair_feedback") if isinstance(meta, dict) else None
    if repair_feedback:
        print(f"  repair_feedback verdict: {repair_feedback.get('verdict')}")
        fc = repair_feedback.get("failed_criteria", [])
        if isinstance(fc, list):
            for c in fc[:3]:
                if isinstance(c, dict):
                    print(f"    feedback FAIL: {c.get('criterion','?')} -> {str(c.get('detail',''))[:200]}")
    source_arts = meta.get("source_artifact_ids") if isinstance(meta, dict) else None
    print(f"  source_artifact_ids: {source_arts}")

# 2. v31 task_runs
print("\n=== task_3__repair_v31 task_runs ===")
cur = conn.execute(
    "SELECT task_id, agent_id, attempt, status, started_at, finished_at, error "
    "FROM task_runs WHERE run_id = ? AND task_id = 'task_3__repair_v31' ORDER BY attempt",
    (RUN_ID,),
)
for r in cur.fetchall():
    d = dict(r)
    print(f"  att={d['attempt']} {d['status']} agent={d['agent_id']} started={d['started_at']} finished={d['finished_at']} err={str(d['error'])[:100]}")

# 3. Recent events for v31 (last 20)
print("\n=== Recent events for task_3__repair_v31 (last 20) ===")
cur = conn.execute(
    "SELECT sequence, event_type, timestamp FROM event_envelopes WHERE run_id = ? AND task_id = 'task_3__repair_v31' ORDER BY sequence DESC LIMIT 20",
    (RUN_ID,),
)
for r in cur.fetchall():
    print(f"  seq={r['sequence']:5d} {r['event_type']:35s} {r['timestamp']}")

# 4. Check tool calls in v31 (from event payloads)
print("\n=== v31 tool calls (last 15) ===")
cur = conn.execute(
    "SELECT sequence, event_type, payload FROM event_envelopes WHERE run_id = ? AND task_id = 'task_3__repair_v31' AND event_type IN ('tool_call_started', 'tool_call_result', 'BeforeToolUse', 'AfterToolUse') ORDER BY sequence DESC LIMIT 15",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    tool = p.get("tool_name") or p.get("tool") or p.get("name") or "?"
    status = p.get("status", "")
    print(f"  seq={r['sequence']:5d} {r['event_type']:25s} tool={tool:20s} status={status}")

# 5. Check elapsed time
print("\n=== v31 elapsed time ===")
cur = conn.execute(
    "SELECT started_at FROM task_runs WHERE run_id = ? AND task_id = 'task_3__repair_v31' AND status = 'running' ORDER BY started_at DESC LIMIT 1",
    (RUN_ID,),
)
r = cur.fetchone()
if r:
    print(f"  started_at: {r['started_at']}")

conn.close()
