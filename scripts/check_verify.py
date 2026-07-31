"""Check verification details."""
import json
import urllib.request

RUN_ID = "run_bd578dc5ec33453d"
BASE = "http://localhost:8081"

with urllib.request.urlopen(f"{BASE}/api/v1/runs/{RUN_ID}/events?limit=500", timeout=30) as resp:
    events = json.loads(resp.read().decode("utf-8"))

for ev in events:
    if ev.get("event_type") == "VerificationCompleted":
        print(f"seq={ev['sequence']}")
        payload = ev.get("payload", {})
        verification = payload.get("verification", payload)
        print(json.dumps(verification, indent=2, ensure_ascii=False))
        print("---")
