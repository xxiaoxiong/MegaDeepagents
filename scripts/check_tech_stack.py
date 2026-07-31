"""Check tech stack consistency across repair chain by examining artifact file extensions."""
import json
import sqlite3
from collections import Counter

DB = "/data/app.sqlite3"
RUN_ID = "run_de866d4e976b4c3a"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Get all tasks and their artifacts
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? ORDER BY updated_at",
    (RUN_ID,),
)

print("=== Tech stack consistency analysis ===\n")
for r in cur.fetchall():
    tid = r["task_id"]
    p = json.loads(r["payload"]) if r["payload"] else {}
    produced = p.get("produced_artifact_ids", []) or []
    if not produced:
        continue
    st = p.get("status", "?")
    meta = p.get("metadata", {}) or {}
    verif = meta.get("verification", {}) if isinstance(meta, dict) else {}
    v = verif.get("verdict", "-") if verif else "-"

    # Get artifact file extensions
    exts = Counter()
    file_list = []
    for aid in produced:
        acur = conn.execute(
            "SELECT relative_path FROM artifacts WHERE artifact_id = ?",
            (aid,),
        )
        ar = acur.fetchone()
        if ar:
            path = ar["relative_path"]
            fname = path.split("/")[-1] if path else "?"
            ext = "." + fname.rsplit(".", 1)[-1] if "." in fname else "(no ext)"
            exts[ext] += 1
            file_list.append(fname)

    ext_summary = ", ".join(f"{ext}({cnt})" for ext, cnt in exts.most_common())
    print(f"{tid:30s} status={st:18s} v={v:10s} exts=[{ext_summary}]")
    # Show first few files
    for f in file_list[:4]:
        print(f"  - {f}")
    if len(file_list) > 4:
        print(f"  ... and {len(file_list)-4} more")

# Specifically check task_2 chain for framework switches
print("\n=== task_2 repair chain framework analysis ===")
cur = conn.execute(
    "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id LIKE 'task_2%' ORDER BY updated_at",
    (RUN_ID,),
)
for r in cur.fetchall():
    tid = r["task_id"]
    p = json.loads(r["payload"]) if r["payload"] else {}
    produced = p.get("produced_artifact_ids", []) or []
    st = p.get("status", "?")
    exts = set()
    for aid in produced:
        acur = conn.execute(
            "SELECT relative_path FROM artifacts WHERE artifact_id = ?",
            (aid,),
        )
        ar = acur.fetchone()
        if ar:
            path = ar["relative_path"]
            fname = path.split("/")[-1] if path else "?"
            if "." in fname:
                exts.add("." + fname.rsplit(".", 1)[-1])
    ext_str = ", ".join(sorted(exts)) if exts else "(none)"
    print(f"  {tid:30s} status={st:18s} extensions={ext_str}")

conn.close()
