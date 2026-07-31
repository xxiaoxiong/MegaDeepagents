"""Inspect the current state of the runtime database and latest runs."""
import sqlite3
import json
import os


def main() -> None:
    db_path = "/data/app.sqlite3"
    if not os.path.exists(db_path):
        print(f"DB not found at {db_path}")
        return
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # task_runs: latest activity
    print("=== Latest task_runs (most recent 15) ===")
    cur = conn.execute(
        "SELECT task_run_id, task_id, agent_id, run_id, attempt, status, "
        "started_at, finished_at, error FROM task_runs "
        "ORDER BY started_at DESC LIMIT 15"
    )
    for r in cur.fetchall():
        d = dict(r)
        print(f"  {d['task_id']:30s} attempt={d['attempt']} status={d['status']:10s} "
              f"agent={d['agent_id']} err={str(d['error'])[:80]}")

    # task_board_tasks with full payload
    print("\n=== task_board_tasks for latest run ===")
    cur = conn.execute(
        "SELECT run_id, task_id, payload, updated_at FROM task_board_tasks "
        "ORDER BY updated_at DESC LIMIT 30"
    )
    rows = cur.fetchall()
    # Group by run_id, pick the latest run
    run_ids = []
    seen = set()
    for r in rows:
        rid = r["run_id"]
        if rid not in seen:
            seen.add(rid)
            run_ids.append(rid)
    latest_run = run_ids[0] if run_ids else None
    print(f"Latest run_id: {latest_run}")

    cur = conn.execute(
        "SELECT task_id, payload, updated_at FROM task_board_tasks "
        "WHERE run_id = ? ORDER BY updated_at",
        (latest_run,),
    )
    for r in cur.fetchall():
        tid = r["task_id"]
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            payload = {}
        status = payload.get("status", "?")
        agent = payload.get("assigned_agent_id") or payload.get("agent_id") or "-"
        attempts = payload.get("attempts", "?")
        caps = payload.get("required_capabilities", "?")
        max_attempts = payload.get("max_attempts", "?")
        meta = payload.get("metadata", {}) or {}
        verif = meta.get("verification", {}) if isinstance(meta, dict) else {}
        produced = payload.get("produced_artifact_ids", []) or []
        print(f"\n  {tid}")
        print(f"    status={status} agent={agent} attempts={attempts}/{max_attempts} caps={caps}")
        print(f"    produced_artifacts={produced}")
        if verif:
            verdict = verif.get("verdict")
            summary = str(verif.get("summary", ""))[:300]
            fc = verif.get("failed_criteria", [])
            print(f"    verification: verdict={verdict}")
            print(f"      summary={summary}")
            if isinstance(fc, list):
                for c in fc[:5]:
                    if isinstance(c, dict):
                        print(f"      failed: {c.get('criterion','?')} -> {str(c.get('detail',''))[:200]}")
                    else:
                        print(f"      failed: {str(c)[:200]}")

    # team_runs / overall run status
    print("\n=== team_runs ===")
    try:
        cur = conn.execute("PRAGMA table_info(team_runs)")
        cols = [r[1] for r in cur.fetchall()]
        print("Columns:", cols)
        cur = conn.execute("SELECT * FROM team_runs ORDER BY rowid DESC LIMIT 5")
        for r in cur.fetchall():
            d = dict(r)
            for k, v in list(d.items()):
                if isinstance(v, str) and len(v) > 200:
                    d[k] = v[:200] + "..."
            print(json.dumps(d, ensure_ascii=False, default=str))
    except Exception as e:
        print(f"team_runs error: {e}")

    # Count tool_calls by status for latest run
    print("\n=== tool_calls summary for latest run ===")
    try:
        cur = conn.execute(
            "SELECT status, COUNT(*) as n FROM tool_calls WHERE run_id = ? GROUP BY status",
            (latest_run,),
        )
        for r in cur.fetchall():
            print(f"  {r['status']}: {r['n']}")
    except Exception as e:
        print(f"tool_calls error: {e}")

    conn.close()


if __name__ == "__main__":
    main()
