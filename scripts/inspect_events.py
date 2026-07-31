"""Inspect run events: significant events and tail summary."""
import json
import sys


def main() -> None:
    paths = sys.argv[1:]
    if not paths:
        paths = ['/tmp/events1.json', '/tmp/events2.json']
    all_evs = []
    for p in paths:
        try:
            d = json.load(open(p))
        except FileNotFoundError:
            continue
        if isinstance(d, list):
            all_evs.extend(d)
        else:
            all_evs.extend(d.get('items', d.get('events', [])))
    # Dedup by sequence
    seen = set()
    deduped = []
    for e in all_evs:
        s = e.get('sequence')
        if s in seen:
            continue
        seen.add(s)
        deduped.append(e)
    deduped.sort(key=lambda x: x.get('sequence', 0))
    print(f"total_events={len(deduped)}")
    if deduped:
        print(f"last_seq={deduped[-1].get('sequence')}")
        print(f"last_event_type={deduped[-1].get('event_type')}")
    print()

    sig_types = {
        'SchedulerStarted', 'SchedulerStopped', 'SchedulerRoundStarted',
        'TaskCreated', 'TaskClaimed', 'TaskStarted', 'TaskCompleted', 'TaskFailed',
        'VerificationStarted', 'VerificationCompleted',
        'root_graph:dispatch_started', 'root_graph:dispatch_completed',
        'root_graph:repair_planned', 'root_graph:repair_round_exhausted',
        'root_graph:verification_completed', 'root_graph:run_failed',
        'root_graph:run_completed', 'agent_spawned',
        'RunFailed', 'RunCompleted',
    }
    print("=== significant events ===")
    for e in deduped:
        et = e.get('event_type', '') or ''
        if (
            et in sig_types
            or 'Error' in et
            or 'Failed' in et
            or 'Completed' in et
            or 'repair' in et.lower()
        ):
            tid = e.get('task_id', '') or ''
            aid = e.get('agent_id', '') or ''
            print(f"seq={e.get('sequence'):>4} {et:38s} task={tid:30s} agent={aid}")

    print()
    print("=== last 15 events ===")
    for e in deduped[-15:]:
        tid = e.get('task_id', '') or ''
        aid = e.get('agent_id', '') or ''
        print(f"seq={e.get('sequence'):>4} {e.get('event_type',''):38s} task={tid:25s} agent={aid}")

    print()
    print("=== verification results ===")
    for e in deduped:
        et = e.get('event_type', '') or ''
        if et == 'VerificationCompleted':
            payload = e.get('payload', {}) or {}
            v = payload.get('verification', payload)
            fc = v.get('failed_criteria', []) or []
            names = [c.get('criterion', '') for c in fc if isinstance(c, dict)]
            tid = e.get('task_id', '') or ''
            print(f"seq={e.get('sequence'):>4} task={tid:30s} verdict={v.get('verdict','')} failed={len(fc)} criteria={names}")


if __name__ == '__main__':
    main()
