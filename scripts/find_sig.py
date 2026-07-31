"""Find significant state events in events file."""
import json
import sys

d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/events_now.json'))
sig = {
    'TaskFailed', 'TaskCompleted', 'VerificationStarted', 'VerificationCompleted',
    'root_graph:repair_planned', 'root_graph:repair_round_exhausted',
    'root_graph:run_failed', 'root_graph:run_completed',
    'RunFailed', 'RunCompleted',
}
for e in d:
    et = e.get('event_type', '') or ''
    if et in sig:
        p = e.get('payload', {}) or {}
        verdict = p.get('verdict', '')
        error = str(p.get('error', ''))[:120]
        tid = e.get('task_id', '') or ''
        print(f"seq={e.get('sequence')} {et} task={tid} verdict={verdict} error={error}")
