"""Dump run events for a given run_id."""
import json
import sqlite3
import sys


def main(run_id: str, db_path: str = "/data/app.sqlite3") -> None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    print("== Run Events (chronological) ==")
    rows = con.execute(
        "SELECT event_type, agent_id, task_id, timestamp, payload "
        "FROM event_envelopes WHERE run_id=? ORDER BY timestamp ASC",
        (run_id,),
    ).fetchall()
    for row in rows:
        p = ""
        if row["payload"]:
            try:
                d = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                p = json.dumps(d, ensure_ascii=False)[:200]
            except Exception:
                p = str(row["payload"])[:200]
        aid = (row["agent_id"] or "")[:12]
        tid = row["task_id"] or "-"
        print(f"{row['timestamp']} {row['event_type']:30s} a={aid:13s} t={tid:5s} {p}")
    con.close()


if __name__ == "__main__":
    run_id = sys.argv[1] if len(sys.argv) > 1 else "run_207f813863a04c39"
    db = sys.argv[2] if len(sys.argv) > 2 else "/data/app.sqlite3"
    main(run_id, db)
