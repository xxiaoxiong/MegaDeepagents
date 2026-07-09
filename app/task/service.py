"""任务服务：创建、执行、查询任务。"""

import uuid
from datetime import datetime

from app.core.logging import logger
from app.task.models import ArtifactInfo, Task, TaskEvent, TaskMessage, TaskStatus
from app.task.store import get_task_store


class TaskService:
    def __init__(self):
        self.store = get_task_store()

    def create_task(self, user_input: str, thread_id: str = "default") -> Task:
        task_id = str(uuid.uuid4())[:8]
        now = datetime.utcnow()
        task = Task(
            task_id=task_id,
            user_input=user_input,
            status=TaskStatus.CREATED,
            thread_id=thread_id,
            created_at=now,
            updated_at=now,
        )
        self.store.create_task(task)
        self.store.add_event(task_id, TaskEvent(
            event_type="task_created",
            data={"user_input": user_input},
        ))
        logger.info(f"Task created: {task_id}")
        return task

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get_task(task_id)

    def update_status(self, task_id: str, status: TaskStatus | str) -> Task | None:
        if isinstance(status, str):
            status = TaskStatus(status)
        return self.store.update_task(task_id, status=status)

    def mark_waiting_approval(self, task_id: str) -> Task | None:
        return self.update_status(task_id, TaskStatus.WAITING_APPROVAL)

    def mark_completed(self, task_id: str, final_answer: str = "") -> Task | None:
        return self.store.update_task(task_id, status=TaskStatus.COMPLETED, final_answer=final_answer)

    def mark_failed(self, task_id: str, error_message: str = "") -> Task | None:
        return self.store.update_task(task_id, status=TaskStatus.FAILED, error_message=error_message)

    def mark_cancelled(self, task_id: str) -> Task | None:
        return self.store.update_task(task_id, status=TaskStatus.CANCELLED)

    def add_event(self, task_id: str, event: TaskEvent) -> None:
        self.store.add_event(task_id, event)

    def get_events(self, task_id: str) -> list[TaskEvent]:
        return self.store.get_events(task_id)

    def add_message(self, task_id: str, role: str, content: str, extra: dict | None = None) -> None:
        from app.task.models import TaskMessage
        self.store.add_message(task_id, TaskMessage(
            task_id=task_id,
            role=role,
            content=content,
            extra=extra or {},
        ))

    def get_messages(self, task_id: str) -> list[dict]:
        return [
            {
                "role": m.role,
                "content": m.content,
                "extra": m.extra,
                "created_at": m.created_at.isoformat(),
            }
            for m in self.store.get_messages(task_id)
        ]

    def add_artifact(self, task_id: str, path: str, name: str, size_bytes: int = 0) -> None:
        self.store.add_artifact(task_id, ArtifactInfo(
            path=path, name=name, size_bytes=size_bytes
        ))
        self.store.add_event(task_id, TaskEvent(
            event_type="artifact_created",
            data={"path": path, "name": name},
        ))

    def get_artifacts(self, task_id: str) -> list[ArtifactInfo]:
        return self.store.get_artifacts(task_id)

    def delete_task(self, task_id: str) -> bool:
        return self.store.delete_task(task_id)

    def list_tasks(self, limit: int = 20) -> list[Task]:
        return self.store.list_tasks(limit)


_task_service: TaskService | None = None


def get_task_service() -> TaskService:
    global _task_service
    if _task_service is None:
        _task_service = TaskService()
    return _task_service
