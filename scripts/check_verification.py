"""Check verification data in the SQLite database."""
import sqlite3
import json
import os

# Try both database paths
for db_path in ["/data/app.sqlite3", "/data/megadeepagents.db"]:
    if not os.path.exists(db_path):
        print(f"DB {db_path}: not found")
        continue
    print(f"\n=== DB: {db_path} ===")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # List tables
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    print(f"Tables ({len(tables)}):", tables[:30])

    # Find task-related tables
    task_tables = [t for t in tables if "task" in t.lower() and "board" in t.lower()]
    if not task_tables:
        task_tables = [t for t in tables if "board" in t.lower()]
    if not task_tables:
        task_tables = [t for t in tables if "task" in t.lower()]

    for t in task_tables[:5]:
        print(f"\n--- {t} ---")
        try:
            cur = conn.execute(f"SELECT * FROM {t} WHERE run_id = ?", ("run_3fb3c2572f1348b0",))
            rows = cur.fetchall()
            print(f"  Rows: {len(rows)}")
            for row in rows[:10]:
                d = dict(row)
                tid = d.get("task_id", "?")
                status = d.get("status", "?")
                meta_str = d.get("metadata", "{}")
                try:
                    meta = json.loads(meta_str) if meta_str else {}
                except Exception:
                    meta = {}
                verif = meta.get("verification", {})
                print(f"  {tid} | status={status}")
                if verif:
                    print(f"    verdict: {verif.get('verdict')}")
                    print(f"    summary: {str(verif.get('summary', ''))[:200]}")
                    fc = verif.get("failed_criteria", [])
                    if isinstance(fc, list):
                        for c in fc[:5]:
                            print(f"    failed: {json.dumps(c, ensure_ascii=False)[:300]}")
        except Exception as e:
            print(f"  Error: {e}")

    conn.close()
