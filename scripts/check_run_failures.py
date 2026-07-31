"""Check both runs and why they failed."""
import json
import sqlite3

DB = "/data/app.sqlite3"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

for run_id in ["run_4052c79250a0403c", "run_de866d4e976b4c3a"]:
    print(f"\n=== Run {run_id} ===")
    cur = conn.execute(
        "SELECT status, goal FROM team_runs WHERE run_id = ?",
        (run_id,),
    )
    r = cur.fetchone()
    print(f"  status={r['status']} goal={r['goal'][:30]}")

    # Get final failure events
    cur = conn.execute(
        "SELECT sequence, event_type, payload FROM event_envelopes "
        "WHERE run_id = ? AND event_type IN ('root_graph:run_failed', 'root_graph:repair_round_exhausted', 'RunFailed') "
        "ORDER BY sequence",
        (run_id,),
    )
    for r in cur.fetchall():
        p = json.loads(r["payload"]) if r["payload"] else {}
        print(f"  seq={r['sequence']} {r['event_type']}: {json.dumps(p, ensure_ascii=False)[:200]}")

    # Get task summary
    cur = conn.execute(
        "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id NOT LIKE '%__repair%' ORDER BY task_id",
        (run_id,),
    )
    print("  Original tasks:")
    for r in cur.fetchall():
        p = json.loads(r["payload"]) if r["payload"] else {}
        tid = r["task_id"]
        st = p.get("status", "?")
        caps = p.get("required_capabilities", [])
        print(f"    {tid}: status={st} caps={caps}")

    # Count repairs per base task
    from collections import Counter
    repair_counter = Counter()
    cur = conn.execute(
        "SELECT task_id FROM task_board_tasks WHERE run_id = ? AND task_id LIKE '%__repair%'",
        (run_id,),
    )
    for r in cur.fetchall():
        base = r["task_id"].split("__repair_v")[0]
        repair_counter[base] += 1
    if repair_counter:
        print("  Repair counts:")
        for base, count in sorted(repair_counter.items()):
            print(f"    {base}: {count} repairs")

conn.close()
