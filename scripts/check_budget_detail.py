"""Check tool budget consumption details."""
import urllib.request
import json
import sys

BASE = "http://localhost:8081"
RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run_8dfe5fb9dae74962"


def fetch(path):
    req = urllib.request.Request(BASE + path)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return json.loads(r.read())
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def main():
    evs = fetch(f"/api/v1/runs/{RUN_ID}/events?limit=2000")
    elist = evs if isinstance(evs, list) else evs.get("items", evs.get("events", []))

    # Get all budget consumed events with full payload
    print("=== Budget consumed events (last 10 with full payload) ===")
    budget_events = []
    for e in elist:
        et = e.get("event_type", "") or ""
        if "BudgetConsumed" in et:
            budget_events.append(e)

    for e in budget_events[-10:]:
        p = e.get("payload", {}) or {}
        print(f"  seq={e.get('sequence')} payload={json.dumps(p, ensure_ascii=False)[:200]}")

    # Also check the task_board for the task's budget info
    print("\n=== Tool calls count ===")
    from collections import Counter
    tool_calls = Counter()
    for e in elist:
        seq = e.get("sequence", 0)
        if seq <= 675:
            continue
        et = e.get("event_type", "") or ""
        if "tool_call_started" in et:
            p = e.get("payload", {}) or {}
            tool_calls[p.get("tool_name", "unknown")] += 1

    print(f"Tool calls since restart: {dict(tool_calls)}")
    print(f"Total tool calls: {sum(tool_calls.values())}")

    # Check task status
    print("\n=== Task status ===")
    tasks = fetch(f"/api/v1/runs/{RUN_ID}/tasks")
    items = tasks if isinstance(tasks, list) else tasks.get("items", tasks.get("tasks", []))
    for t in items:
        tid = t.get("task_id") or t.get("id")
        if "repair_v11" in str(tid):
            print(f"  {tid}: status={t.get('status')} claimed_by={t.get('claimed_by')}")


if __name__ == "__main__":
    main()
