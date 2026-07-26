"""Inspect run_2a438328372441d8 state from the container DB.

Usage on host:
    docker cp runtime/inspect_2a438328.py megadeepagents-runtime-1:/tmp/inspect.py
    docker exec megadeepagents-runtime-1 python3 /tmp/inspect.py
"""
from __future__ import annotations

import json
import sqlite3
import sys


def main() -> None:
    conn = sqlite3.connect("/data/app.sqlite3")
    conn.row_factory = sqlite3.Row

    print("=== TABLES ===")
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    print(tables)
    print()

    run_id = "run_2a438328372441d8"

    # team_runs
    print(f"=== team_runs WHERE run_id={run_id} ===")
    try:
        for r in conn.execute("SELECT * FROM team_runs WHERE run_id = ?", (run_id,)):
            print(dict(r))
    except Exception as e:
        print("err:", e)
    print()

    # task_board_tasks
    print("=== task_board_tasks ===")
    try:
        rows = list(conn.execute(
            "SELECT * FROM task_board_tasks WHERE run_id = ? ORDER BY task_id", (run_id,)
        ))
        for r in rows:
            d = dict(r)
            for k in list(d.keys()):
                v = d[k]
                if isinstance(v, str) and len(v) > 600:
                    d[k] = v[:600] + "...[truncated]"
            print(json.dumps(d, ensure_ascii=False, default=str))
        print(f"--- total tasks: {len(rows)}")
    except Exception as e:
        print("err:", e)
    print()

    # task_graph_snapshots
    print("=== task_graph_snapshots ===")
    try:
        rows = list(conn.execute(
            "SELECT * FROM task_graph_snapshots WHERE run_id = ?", (run_id,)
        ))
        for r in rows:
            d = dict(r)
            for k in list(d.keys()):
                v = d[k]
                if isinstance(v, str) and len(v) > 1500:
                    d[k] = v[:1500] + "...[truncated]"
            print(json.dumps(d, ensure_ascii=False, default=str))
        print(f"--- total snapshots: {len(rows)}")
    except Exception as e:
        print("err:", e)
    print()

    # team_events
    print("=== team_events ===")
    try:
        rows = list(conn.execute(
            "SELECT * FROM team_events WHERE run_id = ? ORDER BY id", (run_id,)
        ))
        for r in rows:
            d = dict(r)
            for k in list(d.keys()):
                v = d[k]
                if isinstance(v, str) and len(v) > 800:
                    d[k] = v[:800] + "...[truncated]"
            print(json.dumps(d, ensure_ascii=False, default=str))
        print(f"--- total team_events: {len(rows)}")
    except Exception as e:
        print("err:", e)
    print()

    # event_envelopes (for SchedulerRoundStarted etc.)
    print("=== event_envelopes ===")
    try:
        rows = list(conn.execute(
            "SELECT * FROM event_envelopes WHERE run_id = ? ORDER BY id", (run_id,)
        ))
        for r in rows:
            d = dict(r)
            for k in list(d.keys()):
                v = d[k]
                if isinstance(v, str) and len(v) > 800:
                    d[k] = v[:800] + "...[truncated]"
            print(json.dumps(d, ensure_ascii=False, default=str))
        print(f"--- total event_envelopes: {len(rows)}")
    except Exception as e:
        print("err:", e)
    print()

    # task_runs (per-attempt runs)
    print("=== task_runs ===")
    try:
        rows = list(conn.execute(
            "SELECT * FROM task_runs WHERE run_id = ? ORDER BY id", (run_id,)
        ))
        for r in rows:
            d = dict(r)
            for k in list(d.keys()):
                v = d[k]
                if isinstance(v, str) and len(v) > 600:
                    d[k] = v[:600] + "...[truncated]"
            print(json.dumps(d, ensure_ascii=False, default=str))
        print(f"--- total task_runs: {len(rows)}")
    except Exception as e:
        print("err:", e)
    print()

    # agents
    print("=== agent_instances ===")
    try:
        rows = list(conn.execute(
            "SELECT * FROM agent_instances WHERE run_id = ?", (run_id,)
        ))
        for r in rows:
            d = dict(r)
            for k in list(d.keys()):
                v = d[k]
                if isinstance(v, str) and len(v) > 200:
                    d[k] = v[:200] + "...[truncated]"
            print(json.dumps(d, ensure_ascii=False, default=str))
    except Exception as e:
        print("err:", e)
    print()

    # run_events
    print("=== run_events ===")
    try:
        rows = list(conn.execute(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
        ))
        for r in rows:
            d = dict(r)
            for k in list(d.keys()):
                v = d[k]
                if isinstance(v, str) and len(v) > 500:
                    d[k] = v[:500] + "...[truncated]"
            print(json.dumps(d, ensure_ascii=False, default=str))
        print(f"--- total events: {len(rows)}")
    except Exception as e:
        print("err:", e)


if __name__ == "__main__":
    sys.exit(main())
