"""Cancel the stuck run."""
from app.infrastructure.database.connection import get_connection

conn = get_connection()
conn.execute(
    "UPDATE team_runs SET status='cancelled', updated_at=datetime('now') "
    "WHERE run_id='run_69aefad3ac6a4029'"
)
conn.commit()
print("Run cancelled")
conn.execute(
    "UPDATE task_runs SET status='failed', "
    "error='cancelled by operator - stuck in file read loop', "
    "finished_at=datetime('now') "
    "WHERE run_id='run_69aefad3ac6a4029' AND status='running'"
)
conn.commit()
print("Task runs cancelled")
