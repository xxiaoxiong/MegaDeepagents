"""Check run status, tasks, agents, and recent events."""
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
    run = fetch(f"/api/v1/runs/{RUN_ID}")
    print(f"=== RUN {RUN_ID} ===")
    print(f"status={run.get('status')} error={run.get('error')}")

    print("\n=== TASKS ===")
    tasks = fetch(f"/api/v1/runs/{RUN_ID}/tasks")
    items = tasks if isinstance(tasks, list) else tasks.get("items", tasks.get("tasks", []))
    for t in items:
        tid = t.get("task_id") or t.get("id")
        print(f"  {tid:30} {t.get('status'):16} agent={t.get('claimed_by') or t.get('assigned_agent_id')} caps={t.get('required_capabilities')}")

    print("\n=== AGENTS ===")
    agents = fetch(f"/api/v1/runs/{RUN_ID}/agents")
    aitems = agents if isinstance(agents, list) else agents.get("items", agents.get("agents", []))
    for a in aitems:
        print(f"  {a.get('agent_id')} {a.get('name'):10} status={a.get('status'):10} task={a.get('current_task_id')} caps={a.get('capabilities')}")

    print("\n=== RECENT EVENTS (last 40) ===")
    evs = fetch(f"/api/v1/runs/{RUN_ID}/events?limit=2000")
    elist = evs if isinstance(evs, list) else evs.get("items", evs.get("events", []))
    for e in elist[-40:]:
        et = e.get("event_type", "")
        tid = e.get("task_id", "") or ""
        aid = e.get("agent_id", "") or ""
        p = e.get("payload", {}) or {}
        extra = ""
        if isinstance(p, dict):
            if "verdict" in p:
                extra += f" verdict={str(p.get('verdict'))[:30]}"
            if "failed_criteria" in p:
                extra += f" failed={str(p.get('failed_criteria'))[:80]}"
            if "error" in p:
                extra += f" err={str(p.get('error'))[:80]}"
            if "round" in p:
                extra += f" round={p.get('round')}"
        print(f"  {e.get('sequence','')} {et[:38]:38} task={tid[:26]:26} agent={aid[:16]}{extra}")


if __name__ == "__main__":
    main()
