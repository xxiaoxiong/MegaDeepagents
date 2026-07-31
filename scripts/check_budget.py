"""Check tool budget and artifacts for current task."""
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

    # Budget consumption
    budgets = []
    for e in elist:
        et = e.get("event_type", "") or ""
        if "BudgetConsumed" in et:
            p = e.get("payload", {}) or {}
            budgets.append((e.get("sequence"), p.get("consumed"), p.get("budget"), e.get("task_id")))

    if budgets:
        print("=== Tool budget consumption (last 5) ===")
        for seq, consumed, budget, tid in budgets[-5:]:
            print(f"  seq={seq} task={tid} consumed={consumed} budget={budget}")

    # Artifacts
    print("\n=== Artifacts ===")
    arts = fetch(f"/api/v1/runs/{RUN_ID}/artifacts")
    aitems = arts if isinstance(arts, list) else arts.get("items", arts.get("artifacts", []))
    for a in aitems:
        aid = a.get("artifact_id") or a.get("id")
        print(f"  {aid} task={a.get('task_id')} type={a.get('type')} size={a.get('size_bytes')} name={a.get('name')}")

    # Check for verification of repair_v11
    print("\n=== Verification events for repair_v11 ===")
    for e in elist:
        et = e.get("event_type", "") or ""
        tid = e.get("task_id", "") or ""
        if "repair_v11" in tid and ("erif" in et.lower() or "omplet" in et.lower() or "ailed" in et.lower()):
            p = e.get("payload", {}) or {}
            print(f"  seq={e.get('sequence')} {et} payload={json.dumps(p, ensure_ascii=False)[:200]}")

    # Latest events
    print("\n=== Last 15 events ===")
    for e in elist[-15:]:
        et = e.get("event_type", "") or ""
        tid = e.get("task_id", "") or ""
        p = e.get("payload", {}) or {}
        extra = ""
        if isinstance(p, dict):
            if "consumed" in p:
                extra = f" consumed={p.get('consumed')}/{p.get('budget')}"
            if "tool_name" in p:
                extra = f" tool={p.get('tool_name')}"
            if "verdict" in p:
                extra = f" verdict={p.get('verdict')}"
        print(f"  seq={e.get('sequence')} {et[:30]:30} task={tid[:22]:22}{extra}")


if __name__ == "__main__":
    main()
