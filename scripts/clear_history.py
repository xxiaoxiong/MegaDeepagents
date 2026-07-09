"""清空所有历史会话数据（TRUNCATE 所有任务相关表）。"""
import sqlite3
from pathlib import Path

from app.core.config import settings

db_path = Path(settings.sqlite_path)
if not db_path.exists():
    print(f"数据库不存在: {db_path}")
    raise SystemExit(0)

conn = sqlite3.connect(str(db_path))
cur = conn.cursor()
tables = ["task_messages", "task_events", "artifacts", "skills", "skill_usage_events", "tasks"]
for table in tables:
    try:
        cur.execute(f"DELETE FROM {table}")
        print(f"已清空表: {table}")
    except Exception as e:
        print(f"跳过表 {table}: {e}")
conn.commit()
conn.close()
print("历史会话数据已清空。")
