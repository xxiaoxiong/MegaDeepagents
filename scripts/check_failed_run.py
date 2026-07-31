"""Check the failed run details."""
from __future__ import annotations

import json
from app.infrastructure.database.connection import get_connection

run_id = "run_a88956be58554c3c"
conn = get_connection()

# Run status
print("=== RUN ===")
for r in conn.execute(
    "SELECT run_id, goal, status, created_at, updated_at FROM team_runs WHERE run_id=?",
    (run_id,),
):
    print(dict(r))

# Task board
print("\n=== TASK BOARD ===")
for r in conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id=? ORDER BY rowid",
    (run_id,),
):
    d = dict(r)
    payload = json.loads(d["payload"])
    print(f"  task_id={payload.get('task_id')}")
    print(f"  title={payload.get('title')}")
    print(f"  status={payload.get('status')}")
    print(f"  required_capabilities={payload.get('required_capabilities')}")
    print(f"  objective={payload.get('objective', '')[:100]}")
    print()

# Events
print("\n=== ALL EVENTS ===")
for r in conn.execute(
    "SELECT sequence, event_type, task_id, agent_id, substr(payload, 1, 300) AS preview "
    "FROM event_envelopes WHERE run_id=? ORDER BY sequence",
    (run_id,),
):
    print(dict(r))
