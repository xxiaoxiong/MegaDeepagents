"""Find team_* tool calls in events."""
import json
import sys

d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/events_now.json'))
for e in d:
    p = e.get('payload', {}) or {}
    name = p.get('tool_name', '') or p.get('tool', '') or ''
    if 'team_' in str(name):
        seq = e.get('sequence')
        et = e.get('event_type', '')
        status = p.get('status', '')
        preview = str(p.get('result_preview', ''))[:200]
        print(f'seq={seq} {et} tool={name} status={status} preview={preview}')
