"""Print significant events from an events JSON file."""
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/events_latest.json'
d = json.load(open(path))
evs = d if isinstance(d, list) else d.get('items', [])
print(f'latest_count={len(evs)}')
if evs:
    print(f'first_seq={evs[0].get("sequence")} last_seq={evs[-1].get("sequence")}')

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
for e in evs:
    et = e.get('event_type', '') or ''
    if (
        et in sig_types
        or 'Error' in et
        or 'Failed' in et
        or 'Completed' in et
        or 'repair' in et.lower()
        or et == 'tool_call_result'
    ):
        tid = e.get('task_id', '') or ''
        aid = e.get('agent_id', '') or ''
        p = e.get('payload', {}) or {}
        tool = p.get('tool', '') if isinstance(p, dict) else ''
        status = p.get('status', '') if isinstance(p, dict) else ''
        extra = f' tool={tool} status={status}' if tool else ''
        print(f"seq={e.get('sequence'):>4} {et:30s} task={tid:25s} agent={aid}{extra}")
