"""Inspect a run's persisted state for debugging."""
import json
import sqlite3
import sys
from pathlib import Path


def main(run_id: str, db_path: str = "/data/app.sqlite3") -> None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    print("== Tables ==")
    print(tables)

    # task_board_tasks
    if "task_board_tasks" in tables:
        print("\n== Board Tasks for", run_id, "==")
        for row in con.execute(
            "SELECT task_id, payload FROM task_board_tasks WHERE run_id=? ORDER BY task_id",
            (run_id,),
        ):
            payload = json.loads(row["payload"])
            print(
                f"\n[{row['task_id']}] status={payload.get('status')} "
                f"attempts={payload.get('attempts')}/{payload.get('max_attempts')} "
                f"caps={payload.get('required_capabilities')} "
                f"deps={payload.get('dependencies')} "
                f"next_attempt_at={payload.get('next_attempt_at')}"
            )
            print(f"  title: {payload.get('title','')[:120]}")
            print(f"  objective: {payload.get('objective','')[:200]}")
            if payload.get("last_error"):
                print(f"  last_error: {payload['last_error'][:200]}")
            history = payload.get("metadata", {}).get("error_history", [])
            if history:
                print(f"  error_history:")
                for h in history[-3:]:
                    print(f"    attempt={h.get('attempt')} cat={h.get('category')} msg={h.get('message','')[:160]}")

    # agent_instances
    if "agent_instances" in tables:
        print("\n== Agent Instances for", run_id, "==")
        for row in con.execute(
            "SELECT agent_id, role, status, capabilities, last_heartbeat_at, current_task_id FROM agent_instances WHERE run_id=?",
            (run_id,),
        ):
            print(
                f"  {row['agent_id'][:14]} role={row['role']:15s} status={row['status']:8s} "
                f"task={row['current_task_id']} hb={row['last_heartbeat_at']}"
            )
            print(f"    caps={row['capabilities']}")

    # task_runs
    if "task_runs" in tables:
        print("\n== Task Runs for", run_id, "==")
        for row in con.execute(
            "SELECT task_run_id, task_id, agent_id, attempt, status, error FROM task_runs WHERE run_id=? ORDER BY task_id, attempt",
            (run_id,),
        ):
            print(
                f"  [{row['task_id']}] attempt={row['attempt']} status={row['status']:10s} "
                f"agent={row['agent_id'][:14] if row['agent_id'] else '-'} err={(row['error'] or '')[:120]}"
            )

    # run_events - last 30 for the run
    if "run_events" in tables:
        print("\n== Last 40 Run Events ==")
        for row in con.execute(
            "SELECT event_id, event_type, agent_id, task_id, timestamp, payload FROM run_events WHERE run_id=? ORDER BY timestamp DESC LIMIT 40",
            (run_id,),
        ):
            payload_str = ""
            if row["payload"]:
                try:
                    p = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                    payload_str = json.dumps(p, ensure_ascii=False)[:200]
                except Exception:
                    payload_str = str(row["payload"])[:200]
            print(
                f"  {row['timestamp']} {row['event_type']:30s} "
                f"task={row['task_id'] or '-':5s} {payload_str}"
            )

    con.close()


if __name__ == "__main__":
    run_id = sys.argv[1] if len(sys.argv) > 1 else "run_207f813863a04c39"
    db = sys.argv[2] if len(sys.argv) > 2 else "/data/app.sqlite3"
    main(run_id, db)
