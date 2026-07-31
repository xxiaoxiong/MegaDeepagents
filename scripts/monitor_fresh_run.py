"""Background monitor for run_a3a9f8e5f5004e21.

Polls the run status and events, printing significant lifecycle events
(task started/failed/succeeded, verification, repair, tool errors) so we
can verify multi-agent collaboration and end-to-end completion.
"""
import json
import time
import urllib.request

RUN_ID = "run_a3a9f8e5f5004e21"
BASE = f"http://localhost:8081/api/v1/runs/{RUN_ID}"
INTERVAL = 8
MAX_ITER = 400  # ~53 minutes max

SIG_TYPES = {
    "TaskCreated", "TaskClaimed", "TaskStarted", "TaskProduced",
    "TaskSucceeded", "TaskFailed", "TaskCancelled",
    "VerificationStarted", "VerificationCompleted",
    "SchedulerStarted", "SchedulerStopped",
    "RunCompleted", "RunFailed", "RunCancelled",
    "root_graph:planning_completed", "root_graph:team_build_completed",
    "root_graph:dispatch_started", "root_graph:dispatch_completed",
    "root_graph:repair_planned", "root_graph:repair_round_exhausted",
    "root_graph:run_completed", "root_graph:run_failed",
    "root_graph:verification_completed", "root_graph:verification_precondition_failed",
    "repair_no_candidates", "repair_round_exhausted",
    "ArtifactProduced", "ArtifactStored",
    "no_eligible_worker",
}


def fetch(url: str) -> dict | list:
    try:
        r = urllib.request.urlopen(url, timeout=15)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def main() -> None:
    last_seq = 0
    last_status = None
    print(f"[monitor] start run={RUN_ID} interval={INTERVAL}s", flush=True)
    for i in range(MAX_ITER):
        run = fetch(BASE)
        status = run.get("status") if isinstance(run, dict) else None
        if status != last_status:
            err = run.get("error") if isinstance(run, dict) else None
            print(f"[monitor] iter={i} run_status={status} error={err}", flush=True)
            last_status = status
            if status in {"completed", "failed", "cancelled"}:
                # Print final task summary
                tasks = fetch(f"{BASE}/tasks")
                items = tasks.get("items", tasks) if isinstance(tasks, dict) else tasks
                print(f"[monitor] final tasks ({len(items)}):", flush=True)
                for t in items:
                    tid = t.get("task_id", "?")
                    ts = t.get("status", "?")
                    title = t.get("title", "")[:50]
                    caps = t.get("required_capabilities", [])
                    print(f"  {tid}: {ts} | {title} | caps={caps}", flush=True)
                # Print artifact count
                arts = fetch(f"{BASE}/artifacts")
                a_items = arts.get("items", arts) if isinstance(arts, dict) else arts
                print(f"[monitor] total artifacts: {len(a_items)}", flush=True)
                for a in a_items:
                    print(f"  {a.get('path','?')} ({a.get('size_bytes',0)}b) task={a.get('task_id','?')}", flush=True)
                return

        # Fetch new events
        evs = fetch(f"{BASE}/events?limit=2000&after_sequence={last_seq}")
        if isinstance(evs, list) and evs:
            for e in evs:
                et = e.get("event_type", "") or ""
                seq = e.get("sequence", 0)
                if seq > last_seq:
                    last_seq = seq
                tid = e.get("task_id", "") or ""
                aid = e.get("agent_id", "") or ""
                p = e.get("payload", {}) or {}
                extra = ""
                if "verdict" in p:
                    extra += f" verdict={p['verdict']}"
                if "error" in p and p.get("error"):
                    extra += f" error={str(p['error'])[:120]}"
                if "status" in p:
                    extra += f" status={p['status']}"
                if "failed" in p:
                    extra += f" failed={p['failed']}"
                if "succeeded" in p:
                    extra += f" succeeded={p['succeeded']}"
                if "repair_round" in p:
                    extra += f" round={p['repair_round']}"
                if (
                    et in SIG_TYPES
                    or "Error" in et
                    or "Failed" in et
                    or "Completed" in et
                    or "repair" in et.lower()
                ):
                    print(f"[monitor] seq={seq} {et} task={tid} agent={aid}{extra}", flush=True)
        elif isinstance(evs, dict) and "error" in evs:
            if i % 10 == 0:
                print(f"[monitor] iter={i} events_error={evs['error'][:100]}", flush=True)

        time.sleep(INTERVAL)

    print(f"[monitor] max_iter={MAX_ITER} reached, run still {last_status}", flush=True)


if __name__ == "__main__":
    main()
