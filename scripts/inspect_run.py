"""Inspect verification results and task graph dependencies for a run."""
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
    # Full task details
    print("=== FULL TASK DETAILS ===")
    tasks = fetch(f"/api/v1/runs/{RUN_ID}/tasks")
    items = tasks if isinstance(tasks, list) else tasks.get("items", tasks.get("tasks", []))
    for t in items:
        tid = t.get("task_id") or t.get("id")
        print(f"\n--- {tid} ---")
        print(f"  status: {t.get('status')}")
        print(f"  title: {t.get('title')}")
        print(f"  objective: {(t.get('objective') or '')[:120]}")
        print(f"  required_capabilities: {t.get('required_capabilities')}")
        print(f"  dependencies: {t.get('dependencies')}")
        print(f"  claimed_by: {t.get('claimed_by')}")
        print(f"  attempts: {t.get('attempts')}")
        md = t.get("metadata") or {}
        if isinstance(md, dict):
            if "repair_of" in md:
                print(f"  repair_of: {md.get('repair_of')}")
            if "superseded_by_repair" in md:
                print(f"  superseded_by_repair: {md.get('superseded_by_repair')}")

    # Verification events
    print("\n\n=== VERIFICATION EVENTS ===")
    evs = fetch(f"/api/v1/runs/{RUN_ID}/events?limit=2000")
    elist = evs if isinstance(evs, list) else evs.get("items", evs.get("events", []))
    for e in elist:
        et = e.get("event_type", "") or ""
        if "erif" in et.lower() or "epair" in et.lower() or "ailed" in et.lower() or "omplet" in et.lower():
            tid = e.get("task_id", "") or ""
            p = e.get("payload", {}) or {}
            print(f"\nseq={e.get('sequence')} {et} task={tid}")
            if isinstance(p, dict):
                for k in ("verdict", "failed_criteria", "passed_criteria", "error", "round", "summary", "reason"):
                    if k in p:
                        print(f"  {k}: {str(p.get(k))[:200]}")


if __name__ == "__main__":
    main()
