"""Query verification results directly from the database."""
import sqlite3
import sys

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run_8dfe5fb9dae74962"
con = sqlite3.connect("/data/app.sqlite3")
con.row_factory = sqlite3.Row
cur = con.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cur.fetchall()]
print("TABLES:", tables)

# Find verification-related tables
for t in tables:
    low = t.lower()
    if "erif" in low or "epair" in low or "heck" in low or "ubric" in low:
        print("\n=== TABLE", t, "===")
        cur.execute(f"SELECT * FROM {t} LIMIT 5")
        cols = [d[0] for d in cur.description]
        print("COLUMNS:", cols)
        for row in cur.fetchall():
            d = dict(row)
            for k, v in d.items():
                sv = str(v)
                if len(sv) > 300:
                    sv = sv[:300] + "...[truncated]"
                print(f"  {k}: {sv}")
            print("  ---")

# Also check task_graph rows for capabilities
print("\n\n=== TASK GRAPH (capabilities check) ===")
for t in tables:
    low = t.lower()
    if "task" in low and "graph" in low:
        cur.execute(f"SELECT * FROM {t} WHERE run_id=? OR id LIKE ?", (RUN_ID, f"%{RUN_ID}%"))
        cols = [d[0] for d in cur.description]
        print("TABLE", t, "COLUMNS:", cols)
        for row in cur.fetchall():
            d = dict(row)
            tid = d.get("task_id") or d.get("id") or ""
            if RUN_ID in str(d.values()):
                print(f"  {tid}: caps={d.get('required_capabilities')} deps={d.get('dependencies')} status={d.get('status')}")

con.close()
