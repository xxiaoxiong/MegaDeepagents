"""Dump all assistant messages and tool calls since restart to diagnose the create_file issue."""
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

    print("=== Events since restart (seq > 675) ===")
    for e in elist:
        seq = e.get("sequence", 0)
        if seq <= 675:
            continue
        et = e.get("event_type", "") or ""
        p = e.get("payload", {}) or {}

        if "assistant_message" in et:
            content = p.get("content", "") or ""
            # Show first 200 chars of content
            print(f"\nseq={seq} {et}: {content[:200]}")
        elif "tool_call_started" in et:
            tool = p.get("tool_name", "")
            args = p.get("args") or p.get("arguments") or ""
            if isinstance(args, dict):
                args = json.dumps(args, ensure_ascii=False)
            args_str = str(args)[:150]
            print(f"seq={seq} TOOL_CALL: tool={tool} args={args_str}")
        elif "tool_call_result" in et:
            tool = p.get("tool_name", "")
            status = p.get("status", "")
            result = p.get("result") or p.get("output") or ""
            if isinstance(result, dict):
                result = json.dumps(result, ensure_ascii=False)
            result_str = str(result)[:150]
            print(f"seq={seq} TOOL_RESULT: tool={tool} status={status} result={result_str}")
        elif "BudgetConsumed" in et:
            used = p.get("used", "")
            limit = p.get("limit", "")
            print(f"seq={seq} BUDGET: used={used}/{limit}")


if __name__ == "__main__":
    main()
