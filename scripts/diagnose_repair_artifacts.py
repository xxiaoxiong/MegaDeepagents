"""Diagnose repair task artifact accessibility."""
import json
import sqlite3
import os

DB = "/data/app.sqlite3"
RUN_ID = "run_c120c3aa38dd426d"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Get all task_2 repair tasks and their source_artifact_ids
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id LIKE 'task_2%' ORDER BY updated_at",
    (RUN_ID,),
)

print("=== task_2 repair chain artifact analysis ===\n")
for r in cur.fetchall():
    tid = r["task_id"]
    p = json.loads(r["payload"]) if r["payload"] else {}
    meta = p.get("metadata", {}) or {}
    produced = p.get("produced_artifact_ids", []) or []
    source_arts = meta.get("source_artifact_ids", []) if isinstance(meta, dict) else []
    repair_of = meta.get("repair_of", "") if isinstance(meta, dict) else ""
    vf = meta.get("verification_feedback", {}) if isinstance(meta, dict) else {}
    vf_failed = vf.get("failed_criteria", []) if isinstance(vf, dict) else []

    print(f"--- {tid} ---")
    print(f"  repair_of: {repair_of}")
    print(f"  produced_artifact_ids ({len(produced)}): {produced[:3]}{'...' if len(produced)>3 else ''}")
    print(f"  source_artifact_ids ({len(source_arts)}): {source_arts[:3]}{'...' if len(source_arts)>3 else ''}")
    print(f"  verification failed_criteria: {len(vf_failed)}")

    # Check if source artifacts exist in the artifacts table and their status
    for aid in source_arts[:2]:  # check first 2
        acur = conn.execute(
            "SELECT artifact_id, run_id, task_id, relative_path, status, content_hash FROM artifacts WHERE artifact_id = ?",
            (aid,),
        )
        ar = acur.fetchone()
        if ar:
            print(f"    source artifact {aid}: status={ar['status']} path={ar['relative_path']} task={ar['task_id']}")
            # Check if file exists
            full_path = f"/data/workspaces/{RUN_ID}/{ar['relative_path']}"
            exists = os.path.exists(full_path)
            print(f"      file exists: {exists} at {full_path}")
        else:
            print(f"    source artifact {aid}: NOT FOUND in artifacts table")
    print()

# Also check the artifact_store table schema
print("\n=== Artifacts table schema ===")
cur = conn.execute("PRAGMA table_info(artifacts)")
for r in cur.fetchall():
    print(f"  {r['name']} ({r['type']})")

# Check artifact statuses for task_2 chain
print("\n=== Artifact statuses for task_2 chain ===")
cur = conn.execute(
    "SELECT artifact_id, task_id, relative_path, status FROM artifacts WHERE run_id = ? AND task_id LIKE 'task_2%' ORDER BY task_id, artifact_id",
    (RUN_ID,),
)
for r in cur.fetchall():
    print(f"  {r['artifact_id']:40s} task={r['task_id']:30s} status={r['status']:12s} path={r['relative_path']}")

conn.close()
