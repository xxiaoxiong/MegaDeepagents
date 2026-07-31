"""Check the TaskGraph snapshot for a run to verify output_contract is populated."""
import sqlite3
import json
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else open("/tmp/latest_run_id.txt").read().strip()

conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

# Check task_graph_snapshots
cur = conn.execute("PRAGMA table_info(task_graph_snapshots)")
cols = [r[1] for r in cur.fetchall()]
print("task_graph_snapshots columns:", cols)

cur = conn.execute(
    "SELECT * FROM task_graph_snapshots WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
    (RUN_ID,),
)
row = cur.fetchone()
if not row:
    print("No graph snapshot found")
    conn.close()
    sys.exit(0)

d = dict(row)
# Find the graph data field (likely a JSON blob)
for k in cols:
    v = d.get(k)
    if isinstance(v, str) and len(v) > 100:
        print(f"\nField '{k}' (len={len(v)}):")
        try:
            parsed = json.loads(v)
            if isinstance(parsed, dict):
                # Look for nodes
                nodes = parsed.get("nodes", {})
                if isinstance(nodes, dict):
                    for nid, node in nodes.items():
                        oc = node.get("output_contract", {}) or {}
                        ac = oc.get("acceptance_criteria", []) or []
                        caps = node.get("required_capabilities", [])
                        budget = node.get("budget", {}) or {}
                        max_sec = budget.get("max_seconds", "?")
                        print(f"  {nid:30s} caps={caps} timeout={max_sec}s criteria={len(ac)}")
                        for c in ac[:3]:
                            print(f"    - {str(c)[:90]}")
                else:
                    print("  (no nodes dict in graph)")
        except Exception as e:
            print(f"  parse error: {e}")
            print(f"  raw[:200]: {v[:200]}")
    else:
        print(f"Field '{k}': {v}")

conn.close()
