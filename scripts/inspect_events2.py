import json
import sys
import urllib.request

run_id = sys.argv[1] if len(sys.argv) > 1 else 'run_b15aed026dcc4b43'
url = f'http://localhost:8081/api/v1/runs/{run_id}/events?limit=10000'
with urllib.request.urlopen(url) as r:
    d = json.load(r)
evs = d if isinstance(d, list) else d.get('items', d.get('events', []))
print('total_events=', len(evs))
if evs:
    print('last_seq=', evs[-1].get('sequence'))
    print('last_event_type=', evs[-1].get('event_type'))
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
print('=== significant events (last 100) ===')
shown = 0
for e in evs:
    et = e.get('event_type', '')
    if et in sig_types or 'Error' in et or 'Failed' in et or 'Completed' in et or 'repair' in et.lower():
        tid = e.get('task_id', '')
        aid = e.get('agent_id', '')
        print(f"seq={e.get('sequence'):>4} {et:38s} task={tid:30s} agent={aid}")
        shown += 1
        if shown >= 100:
            break
