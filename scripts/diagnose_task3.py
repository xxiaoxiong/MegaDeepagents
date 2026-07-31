"""Diagnose task_3 repair chain non-convergence in run_e705290b97cf4a14."""
import sqlite3
import json

RUN_ID = "run_e705290b97cf4a14"
conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

# 1. Check graph snapshot schema and repair rounds
print("=== Graph snapshots ===")
cur = conn.execute("PRAGMA table_info(task_graph_snapshots)")
cols = [r["name"] for r in cur.fetchall()]
print(f"columns: {cols}")

cur = conn.execute(
    "SELECT * FROM task_graph_snapshots WHERE run_id = ? ORDER BY version DESC LIMIT 1",
    (RUN_ID,),
)
r = cur.fetchone()
if r:
    d = dict(r)
    # Find the JSON column
    for col in cols:
        val = d.get(col)
        if isinstance(val, str) and val.startswith("{"):
            g = json.loads(val)
            print(f"  version={g.get('version')} max_repair_rounds={g.get('max_repair_rounds', 'n/a')}")
            nodes = g.get("nodes", {})
            print(f"  total_nodes={len(nodes)}")
            # Show task_3 and its repair chain
            for nid, node in nodes.items():
                if nid == "task_3" or "__repair" in nid:
                    oc = node.get("output_contract", {})
                    ac = oc.get("acceptance_criteria", [])
                    print(f"  {nid:35s} status={node.get('status'):18s} criteria={ac[:2]}")
            break

# 2. Detailed verification failures for task_3 chain
print("\n=== task_3 repair chain verification details ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND (task_id LIKE 'task_3%' OR task_id = 'task_3') ORDER BY task_id",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    meta = p.get("metadata", {}) or {}
    verif = meta.get("verification", {}) if isinstance(meta, dict) else {}
    st = p.get("status", "?")
    if verif:
        v = verif.get("verdict", "?")
        fc = verif.get("failed_criteria", [])
        summary = verif.get("summary", "")
        print(f"\n  {r['task_id']} ({st}, v={v})")
        print(f"    summary: {summary[:200]}")
        if isinstance(fc, list):
            for c in fc[:5]:
                if isinstance(c, dict):
                    print(f"    FAIL: {c.get('criterion','?')} (severity={c.get('severity','?')}) -> {str(c.get('detail',''))[:150]}")
                else:
                    print(f"    FAIL: {str(c)[:150]}")

# 3. Check task_3 output contract
print("\n=== task_3 original output contract ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id = 'task_3'",
    (RUN_ID,),
)
r = cur.fetchone()
if r:
    p = json.loads(r["payload"]) if r["payload"] else {}
    print(f"  objective: {p.get('objective','')[:200]}")
    print(f"  description: {p.get('description','')[:200]}")
    print(f"  caps: {p.get('required_capabilities', [])}")

# 4. Artifacts produced by task_3 chain
print("\n=== Artifacts produced by task_3 chain ===")
cur = conn.execute(
    "SELECT artifact_id, task_id, path, size_bytes, status FROM artifacts WHERE run_id = ? AND (task_id LIKE 'task_3%') ORDER BY task_id, artifact_id",
    (RUN_ID,),
)
for r in cur.fetchall():
    print(f"  {r['task_id']:35s} {r['artifact_id']:20s} {r['path']:50s} size={r['size_bytes']:8d} status={r['status']}")

# 5. Check the repair feedback being passed
print("\n=== task_3__repair_v31 (current running) details ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id = 'task_3__repair_v31'",
    (RUN_ID,),
)
r = cur.fetchone()
if r:
    p = json.loads(r["payload"]) if r["payload"] else {}
    print(f"  status: {p.get('status')}")
    print(f"  objective: {p.get('objective','')[:300]}")
    print(f"  description: {p.get('description','')[:300]}")
    meta = p.get("metadata", {}) or {}
    repair_of = meta.get("repair_of") if isinstance(meta, dict) else None
    repair_round = meta.get("repair_round") if isinstance(meta, dict) else None
    print(f"  repair_of: {repair_of} round: {repair_round}")

conn.close()
