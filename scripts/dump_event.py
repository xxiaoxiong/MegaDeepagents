"""Dump full payload of specific events by sequence number."""
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/events1.json'
target_seqs = {int(x) for x in sys.argv[2:]} if len(sys.argv) > 2 else None

paths = [path]
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

# Also load events2 if available
try:
    d2 = json.load(open('/tmp/events2.json'))
    if isinstance(d2, list):
        all_evs.extend(d2)
except FileNotFoundError:
    pass

for e in all_evs:
    if target_seqs is None or e.get('sequence') in target_seqs:
        print(f"--- seq={e.get('sequence')} {e.get('event_type','')} ---")
        print(json.dumps(e, indent=2, default=str, ensure_ascii=False))
        print()
