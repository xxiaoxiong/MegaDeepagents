"""Inspect task_4's output contract and artifacts to understand testing verification gap."""
import sqlite3
import json


def main() -> None:
    conn = sqlite3.connect("/data/app.sqlite3")
    conn.row_factory = sqlite3.Row
    run_id = "run_3fb3c2572f1348b0"

    # Get task_4 payload with full contract
    print("=== task_4 full payload ===")
    cur = conn.execute(
        "SELECT task_id, payload FROM task_board_tasks WHERE run_id = ? AND task_id LIKE 'task_4%' ORDER BY updated_at",
        (run_id,),
    )
    for r in cur.fetchall():
        tid = r["task_id"]
        try:
            p = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            p = {}
        oc = p.get("output_contract", {})
        print(f"\n--- {tid} ---")
        print(f"  status={p.get('status')}")
        print(f"  caps={p.get('required_capabilities')}")
        print(f"  objective={p.get('objective','')[:300]}")
        print(f"  output_contract:")
        print(f"    artifact_type={oc.get('artifact_type')}")
        print(f"    description={str(oc.get('description',''))[:200]}")
        print(f"    acceptance_criteria={json.dumps(oc.get('acceptance_criteria',[]), ensure_ascii=False)[:500]}")
        print(f"    required_artifacts={oc.get('required_artifacts')}")
        print(f"  produced_artifact_ids={p.get('produced_artifact_ids')}")

    # Look up artifact details for task_4's produced artifacts
    print("\n=== artifacts table schema ===")
    cur = conn.execute("PRAGMA table_info(artifacts)")
    cols = [r[1] for r in cur.fetchall()]
    print("Columns:", cols)

    print("\n=== task_4 artifacts detail ===")
    art_ids = ["art_19ea9e14e18b43c9", "art_49bf24c4f4f848a3"]
    for aid in art_ids:
        cur = conn.execute("SELECT * FROM artifacts WHERE artifact_id = ?", (aid,))
        row = cur.fetchone()
        if row:
            d = dict(row)
            for k, v in list(d.items()):
                if isinstance(v, str) and len(v) > 300:
                    d[k] = v[:300] + "..."
            print(json.dumps(d, ensure_ascii=False, default=str))
        else:
            print(f"  {aid}: NOT FOUND")

    # List ALL artifacts for this run
    print("\n=== All artifacts for run ===")
    cur = conn.execute(
        "SELECT artifact_id, produced_by, path, type, size_bytes FROM artifacts WHERE run_id = ? ORDER BY rowid",
        (run_id,),
    )
    for r in cur.fetchall():
        d = dict(r)
        print(f"  {d['artifact_id']} | by={d.get('produced_by','?')} | type={d.get('type','?')} | size={d.get('size_bytes','?')} | path={d.get('path','?')}")

    conn.close()


if __name__ == "__main__":
    main()
