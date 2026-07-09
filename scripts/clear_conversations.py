"""清空历史会话记录（保留 Skills 等核心表）。"""

from app.task.store import get_task_store


def clear_conversations():
    store = get_task_store()
    conn = store.conn

    conn.execute("DELETE FROM task_messages")
    conn.execute("DELETE FROM task_events")
    conn.execute("DELETE FROM artifacts")
    conn.execute("DELETE FROM tasks")
    conn.commit()

    # 重置 SQLite 自增 ID
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('task_messages', 'task_events', 'artifacts', 'tasks')")
    conn.commit()

    print("已清空所有会话记录")


if __name__ == "__main__":
    clear_conversations()
