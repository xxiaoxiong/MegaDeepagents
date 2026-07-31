"""Dump full verification event payloads."""
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
    for e in elist:
        et = e.get("event_type", "") or ""
        if "VerificationCompleted" in et or "VerificationStarted" in et:
            tid = e.get("task_id", "") or ""
            p = e.get("payload", {}) or {}
            print(f"\n=== seq={e.get('sequence')} {et} task={tid} ===")
            print(json.dumps(p, indent=2, ensure_ascii=False, default=str))

    # Also dump artifacts for task_1
    print("\n\n=== ARTIFACTS ===")
    arts = fetch(f"/api/v1/runs/{RUN_ID}/artifacts")
    aitems = arts if isinstance(arts, list) else arts.get("items", arts.get("artifacts", []))
    for a in aitems:
        print(f"  {a.get('artifact_id') or a.get('id')} task={a.get('task_id')} name={a.get('name')} type={a.get('type')} size={a.get('size_bytes')}")


if __name__ == "__main__":
    main()
