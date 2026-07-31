"""Start a fresh end-to-end verification run."""
import json
import time
import urllib.request

BASE_URL = "http://127.0.0.1:8081"

GOAL = "构建一个前后端项目"


def start_run() -> str:
    """Start a new team run via the API."""
    payload = json.dumps({"goal": GOAL, "mode": "auto"}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/runs",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    run_id = data.get("run_id") or data.get("id")
    print(f"Started run: {run_id}")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return run_id


if __name__ == "__main__":
    run_id = start_run()
    # Save run_id for monitoring
    with open("/tmp/current_run_id.txt", "w") as f:
        f.write(run_id)
    print(f"\nRun ID saved to /tmp/current_run_id.txt")
