"""Dump tool calls for a run to check for failures."""
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

    # Collect tool calls
    tool_events = []
    for e in elist:
        et = e.get("event_type", "") or ""
        if "ool" in et or "ction" in et:
            p = e.get("payload", {}) or {}
            tool_events.append((e.get("sequence"), et, e.get("task_id", ""), p))

    print(f"Total tool-related events: {len(tool_events)}")
    print("\n=== Last 30 tool events ===")
    for seq, et, tid, p in tool_events[-30:]:
        name = p.get("tool_name") or p.get("name") or ""
        status = p.get("status") or ""
        error = p.get("error") or ""
        args = p.get("args") or p.get("arguments") or ""
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        args_str = str(args)[:80]
        print(f"  seq={seq} {et[:25]:25} task={tid[:20]:20} tool={name[:15]:15} status={status[:10]:10} err={str(error)[:50]:50} args={args_str}")

    # Count failures by tool
    print("\n=== Tool call summary ===")
    from collections import Counter
    success = Counter()
    failure = Counter()
    for seq, et, tid, p in tool_events:
        name = p.get("tool_name") or p.get("name") or "unknown"
        status = p.get("status") or ""
        error = p.get("error") or ""
        if "error" in str(status).lower() or "fail" in str(status).lower() or error:
            failure[name] += 1
        elif "success" in str(status).lower() or "ok" in str(status).lower():
            success[name] += 1
    print("Successes:", dict(success))
    print("Failures:", dict(failure))


if __name__ == "__main__":
    main()
