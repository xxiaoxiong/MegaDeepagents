"""Check tool calls since restart to see if planner is productive."""
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

    # Tool calls since restart (seq > 675)
    print("=== Tool calls since restart (seq > 675) ===")
    from collections import Counter
    tool_counts = Counter()
    create_files = []
    for e in elist:
        seq = e.get("sequence", 0)
        if seq <= 675:
            continue
        et = e.get("event_type", "") or ""
        p = e.get("payload", {}) or {}
        if "tool_call_result" in et:
            tool = p.get("tool_name", "unknown")
            status = p.get("status", "")
            tool_counts[f"{tool}:{status}"] += 1
        if "tool_call_started" in et:
            tool = p.get("tool_name", "")
            args = p.get("args") or ""
            if isinstance(args, str) and len(args) > 80:
                args = args[:80]
            if tool == "create_file":
                create_files.append((seq, args))

    print("Tool call results:", dict(tool_counts))
    print(f"\nCreate file calls: {len(create_files)}")
    for seq, args in create_files:
        print(f"  seq={seq} args={args}")

    # Check for any errors
    print("\n=== Error events since restart ===")
    for e in elist:
        seq = e.get("sequence", 0)
        if seq <= 675:
            continue
        et = e.get("event_type", "") or ""
        p = e.get("payload", {}) or {}
        if isinstance(p, dict) and p.get("error"):
            print(f"  seq={seq} {et} error={str(p.get('error'))[:100]}")

    # Artifacts
    print("\n=== Artifacts ===")
    arts = fetch(f"/api/v1/runs/{RUN_ID}/artifacts")
    aitems = arts if isinstance(arts, list) else arts.get("items", arts.get("artifacts", []))
    for a in aitems:
        aid = a.get("artifact_id") or a.get("id")
        print(f"  {aid} task={a.get('task_id')} type={a.get('type')} size={a.get('size_bytes')}")


if __name__ == "__main__":
    main()
