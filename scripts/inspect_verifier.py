"""Dig into why LLM rubric verifier failed for task_3 repairs."""
import sqlite3
import json


def main() -> None:
    conn = sqlite3.connect("/data/app.sqlite3")
    conn.row_factory = sqlite3.Row

    run_id = "run_3fb3c2572f1348b0"

    # Look at event envelopes for verifier-related events
    print("=== event_envelopes schema ===")
    cur = conn.execute("PRAGMA table_info(event_envelopes)")
    cols = [r[1] for r in cur.fetchall()]
    print("Columns:", cols)

    # Find verification-related events
    print("\n=== Verification-related events ===")
    cur = conn.execute(
        "SELECT * FROM event_envelopes WHERE run_id = ? "
        "AND (payload LIKE '%verif%' OR payload LIKE '%rubric%' OR payload LIKE '%semantic%') "
        "ORDER BY rowid DESC LIMIT 20",
        (run_id,),
    )
    for r in cur.fetchall():
        d = dict(r)
        payload = d.get("payload", "{}")
        try:
            p = json.loads(payload) if isinstance(payload, str) else payload
        except Exception:
            p = {"raw": str(payload)[:300]}
        etype = p.get("event_type", d.get("event_type", "?"))
        ts = d.get("created_at", "?")
        print(f"\n[{ts}] {etype}")
        # Print key fields
        for k in ("task_id", "verdict", "summary", "error", "failed_criteria"):
            if k in p:
                v = p[k]
                if isinstance(v, (list, dict)):
                    print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:400]}")
                else:
                    print(f"  {k}: {str(v)[:300]}")

    # Look at agent_messages for LLM errors around task_3__repair_v15/v19
    print("\n=== agent_messages schema ===")
    cur = conn.execute("PRAGMA table_info(agent_messages)")
    cols = [r[1] for r in cur.fetchall()]
    print("Columns:", cols)

    # Find error messages
    print("\n=== Recent agent_messages with errors ===")
    cur = conn.execute(
        "SELECT * FROM agent_messages WHERE run_id = ? "
        "AND (content LIKE '%error%' OR content LIKE '%Error%' OR content LIKE '%429%' "
        "OR content LIKE '%timeout%' OR content LIKE '%semantic_verifier%') "
        "ORDER BY rowid DESC LIMIT 10",
        (run_id,),
    )
    for r in cur.fetchall():
        d = dict(r)
        content = str(d.get("content", ""))[:400]
        print(f"  agent={d.get('agent_id','?')} task={d.get('task_id','?')} | {content}")

    conn.close()


if __name__ == "__main__":
    main()
