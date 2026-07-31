"""Check max_repair_rounds enforcement and artifacts schema."""
import sqlite3
import json

RUN_ID = "run_e705290b97cf4a14"
conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

# 1. Artifacts schema
print("=== Artifacts schema ===")
cur = conn.execute("PRAGMA table_info(artifacts)")
for r in cur.fetchall():
    print(f"  {r['name']} ({r['type']})")

# 2. Check graph_json for max_repair_rounds
print("\n=== Graph max_repair_rounds ===")
cur = conn.execute(
    "SELECT version, graph_json FROM task_graph_snapshots WHERE run_id = ? ORDER BY version DESC LIMIT 1",
    (RUN_ID,),
)
r = cur.fetchone()
if r:
    g = json.loads(r["graph_json"])
    # Search for max_repair_rounds anywhere
    print(f"  version={r['version']}")
    print(f"  top-level keys: {list(g.keys())}")
    print(f"  max_repair_rounds (top): {g.get('max_repair_rounds', 'NOT SET')}")
    # Check metadata
    meta = g.get("metadata", {})
    if meta:
        print(f"  metadata: {meta}")
    # Count repair tasks for task_3
    nodes = g.get("nodes", {})
    task3_repairs = [n for n in nodes if "task_3" in n and "__repair" in n]
    print(f"  task_3 repair tasks: {task3_repairs}")

# 3. Check settings for max_repair_rounds
print("\n=== Settings ===")
try:
    cur = conn.execute("SELECT key, value FROM settings WHERE key LIKE '%repair%' OR key LIKE '%round%'")
    for r in cur.fetchall():
        print(f"  {r['key']} = {r['value']}")
except Exception as e:
    print(f"  (no settings table or error: {e})")

# 4. Check the root_graph code for max_repair_rounds default
# Let's also check if repair_round_exhausted event was ever fired for this run
print("\n=== repair_round_exhausted events ===")
cur = conn.execute(
    "SELECT sequence, event_type, task_id, timestamp FROM event_envelopes WHERE run_id = ? AND event_type LIKE '%repair%' ORDER BY sequence",
    (RUN_ID,),
)
for r in cur.fetchall():
    print(f"  seq={r['sequence']} {r['event_type']} task={r['task_id']} ts={r['timestamp']}")

# 5. Artifacts produced by task_3 chain (fix column names)
print("\n=== Artifacts for task_3 chain ===")
cur = conn.execute("PRAGMA table_info(artifacts)")
art_cols = [r["name"] for r in cur.fetchall()]
print(f"  columns: {art_cols}")

# Try to query artifacts with correct columns
if "task_id" in art_cols:
    # Build query with available columns
    select_cols = [c for c in ["artifact_id", "task_id", "file_path", "size_bytes", "status", "artifact_type"] if c in art_cols]
    cur = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM artifacts WHERE run_id = ? AND task_id LIKE 'task_3%' ORDER BY task_id",
        (RUN_ID,),
    )
    for r in cur.fetchall():
        d = dict(r)
        print(f"  {d}")

conn.close()
