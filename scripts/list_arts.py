"""List artifacts with sizes."""
import json
import sys

d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/arts.json'))
arts = d if isinstance(d, list) else d.get('items', d.get('artifacts', []))
print(f'total={len(arts)}')
for a in arts:
    aid = a.get('artifact_id', '')
    tid = a.get('task_id', '')
    status = a.get('status', '')
    path = a.get('path', '')[:50]
    size = a.get('size', a.get('content_length', '?'))
    print(f'  {aid} | task={tid} | status={status} | path={path} | size={size}')
