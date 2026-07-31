"""Check v31 verification details and repair_round_exhausted."""
import sqlite3
import json

RUN_ID = "run_e705290b97cf4a14"
conn = sqlite3.connect("/data/app.sqlite3")
conn.row_factory = sqlite3.Row

# 1. v31 verification details
print("=== task_3__repair_v31 verification ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id = 'task_3__repair_v31'",
    (RUN_ID,),
)
r = cur.fetchone()
if r:
    p = json.loads(r["payload"]) if r["payload"] else {}
    meta = p.get("metadata", {}) or {}
    verif = meta.get("verification", {}) if isinstance(meta, dict) else {}
    if verif:
        v = verif.get("verdict", "?")
        fc = verif.get("failed_criteria", [])
        summary = verif.get("summary", "")
        scores = verif.get("scores", {})
        print(f"  verdict: {v}")
        print(f"  summary: {summary[:300]}")
        print(f"  scores: {scores}")
        if isinstance(fc, list):
            for c in fc[:5]:
                if isinstance(c, dict):
                    print(f"  FAIL: {c.get('criterion','?')} (severity={c.get('severity','?')}) -> {str(c.get('detail',''))[:200]}")
    # Check verification_feedback (what v31 received as input)
    vf = meta.get("verification_feedback", {}) if isinstance(meta, dict) else {}
    if vf:
        print(f"\n  verification_feedback (what v31 received):")
        print(f"    verdict: {vf.get('verdict')}")
        vfc = vf.get("failed_criteria", [])
        if isinstance(vfc, list):
            for c in vfc[:3]:
                if isinstance(c, dict):
                    print(f"    feedback: {c.get('criterion','?')} -> {str(c.get('detail',''))[:200]}")

# 2. repair_round_exhausted event
print("\n=== repair_round_exhausted / repair_planned events ===")
cur = conn.execute(
    "SELECT sequence, event_type, payload, timestamp FROM event_envelopes WHERE run_id = ? AND (event_type LIKE '%repair%') ORDER BY sequence",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    rr = p.get("repair_round", "?")
    print(f"  seq={r['sequence']:5d} {r['event_type']:40s} round={rr} ts={r['timestamp']}")

# 3. Check how the run failed
print("\n=== Run failure events ===")
cur = conn.execute(
    "SELECT sequence, event_type, payload, timestamp FROM event_envelopes WHERE run_id = ? AND event_type IN ('RunFailed', 'run_failed', 'root_graph:failed', 'repair_round_exhausted', 'repair_no_candidates') ORDER BY sequence DESC LIMIT 10",
    (RUN_ID,),
)
for r in cur.fetchall():
    p = json.loads(r["payload"]) if r["payload"] else {}
    print(f"  seq={r['sequence']:5d} {r['event_type']:40s} ts={r['timestamp']}")
    print(f"    payload: {json.dumps(p, ensure_ascii=False)[:300]}")

# 4. v31 artifacts
print("\n=== v31 artifacts ===")
cur = conn.execute(
    "SELECT artifact_id, task_id, relative_path, size_bytes, status FROM artifacts WHERE run_id = ? AND task_id = 'task_3__repair_v31'",
    (RUN_ID,),
)
for r in cur.fetchall():
    print(f"  {r['artifact_id']} {r['relative_path']:50s} size={r['size_bytes']:6d} status={r['status']}")

conn.close()
