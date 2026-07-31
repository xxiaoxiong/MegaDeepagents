"""Launch a new team-mode run for end-to-end verification."""
import json
import sys
import time
import urllib.request

GOAL = "构建一个前后端项目"


def main() -> None:
    payload = {
        "goal": GOAL,
        "mode": "team",
        "team_template": "software_dev_team",
        "review_required": True,
        "auto_approve_low_risk": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:8081/api/v1/runs",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            print("Run created:")
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            run_id = result.get("run_id") or result.get("id")
            if run_id:
                with open("/tmp/latest_run_id.txt", "w") as f:
                    f.write(run_id)
                print(f"\nrun_id={run_id} written to /tmp/latest_run_id.txt")
    except Exception as e:
        print(f"Error launching run: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
