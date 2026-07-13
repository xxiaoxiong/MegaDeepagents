"""TeamRunContext — 全链路统一 Run 上下文。

docs/MegaDeepagents_Agent_Teams_改造任务书.md §7：
- 从 CLI/API 入口创建一次。
- 全链路显式传递。
- 禁止中间组件使用 `cli_run`、`default_run` 等固定生产回退值。
- 所有 Task、Agent、Artifact、Checkpoint 必须关联同一个 Run ID。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class TeamRunMode(str, Enum):
    """团队运行模式。"""
    TASK_TEAM = "task_team"
    DISCUSSION = "discussion"

    # 兼容原有 mode 值
    CONTROLLED_GROUP_CHAT = "controlled_group_chat"

    @classmethod
    def from_legacy(cls, mode: str) -> "TeamRunMode":
        mapping = {
            "controlled_group_chat": cls.DISCUSSION,
            "round_robin": cls.DISCUSSION,
            "free_form": cls.DISCUSSION,
            "full_multi": cls.TASK_TEAM,
            "task_team": cls.TASK_TEAM,
            "discussion": cls.DISCUSSION,
        }
        return mapping.get(mode, cls.TASK_TEAM)


class TeamRunContext(BaseModel):
    """表示一个团队运行的全生命周期上下文。

    所有组件都必须通过显式参数接收此对象，不得从全局变量或硬编码字符串获取。
    """

    run_id: str
    team_id: str
    mode: TeamRunMode = TeamRunMode.TASK_TEAM

    workspace_root: str
    artifact_store_id: str | None = None
    checkpoint_namespace: str

    user_goal: str = ""
    trace_id: str | None = None
    user_id: str | None = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ===== Workspace helpers =====

    def task_workspace(self, task_id: str) -> str:
        """Task 专属工作目录（自动创建）。"""
        path = os.path.join(self.workspace_root, "tasks", task_id)
        os.makedirs(path, exist_ok=True)
        return path

    def task_relative_path(self, task_id: str, relative: str) -> str:
        """构造 task 产物相对路径（用于 ArtifactStore）。"""
        safe_task = task_id.replace("/", "_").replace("\\", "_")
        return f"tasks/{safe_task}/{relative}"

    def shared_dir(self) -> str:
        path = os.path.join(self.workspace_root, "shared")
        os.makedirs(path, exist_ok=True)
        return path

    def artifacts_dir(self) -> str:
        path = os.path.join(self.workspace_root, "artifacts")
        os.makedirs(path, exist_ok=True)
        return path

    def checkpoints_dir(self) -> str:
        path = os.path.join(self.workspace_root, "checkpoints")
        os.makedirs(path, exist_ok=True)
        return path

    def checkpoint_path(self, name: str = "team.sqlite3") -> str:
        return os.path.join(self.checkpoints_dir(), name)

    # ===== Factory =====

    @classmethod
    def create(
        cls,
        goal: str,
        team_name: str = "software_dev_team",
        mode: TeamRunMode = TeamRunMode.TASK_TEAM,
        workspace_root: str | None = None,
        user_id: str | None = None,
    ) -> "TeamRunContext":
        """创建新 Run 上下文（包含 workspace 目录初始化）。"""
        run_id = "run_" + uuid.uuid4().hex[:16]

        if not workspace_root:
            workspace_root = str(
                Path(os.getcwd()) / "runtime" / "workspaces" / run_id
            )

        ctx = cls(
            run_id=run_id,
            team_id=team_name,
            mode=mode,
            workspace_root=workspace_root,
            checkpoint_namespace=f"team:{run_id}",
            user_goal=goal,
            user_id=user_id,
        )

        # 初始化目录
        os.makedirs(ctx.workspace_root, exist_ok=True)
        os.makedirs(ctx.artifacts_dir(), exist_ok=True)
        os.makedirs(ctx.checkpoints_dir(), exist_ok=True)

        return ctx
