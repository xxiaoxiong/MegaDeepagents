"""TeamRunContext：统一 Run 级上下文（Phase A+B 基础设施）。

设计目的：
- 把一次"团队运行"所需的所有 Run 级元信息（run_id / team_id / workspace_root /
  artifact store 引用 / checkpoint namespace / trace 关联 / 用户身份）封装成一个
  不可变-ish 的 Pydantic 对象，沿整个调用链向下传递。
- CLI / API / Web 三个入口通过 TeamRuntimeFacade 创建并消费它，避免各路径
  自己维护 run_id / workspace 等字符串的拼接逻辑（历史上"cli_run"硬编码就来自
  此处遗漏）。
- 与 app/multiagent/policies.py 中的 TeamRunMode（CONTROLLED_GROUP_CHAT /
  ROUND_ROBIN / FREE_FORM）属于不同维度：
    * policies.TeamRunMode 描述"讨论阶段的发言控制方式"
    * 本模块 TeamRunMode 描述"整次 Run 是任务团队模式还是讨论模式"
  两者可在未来扩展中组合（如 DISCUSSION + ROUND_ROBIN）。
"""
from __future__ import annotations

import os
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TeamRunMode(str, Enum):
    """整次 Run 的顶层模式。"""

    TASK_TEAM = "task_team"        # 走 Phase Two 任务图编排（Orchestrator + Scheduler）
    DISCUSSION = "discussion"      # 走传统多 Agent 群聊（TeamRunner）


class TeamRunContext(BaseModel):
    """Unified Run context - passed through the entire chain.

    所有 Phase A+B 之后的入口（TeamRuntimeFacade.create_run/start_run）都会构造
    此对象，并在 orchestrator / executor / verifier / artifact_store 之间共享。
    """

    run_id: str
    team_id: str
    mode: TeamRunMode = TeamRunMode.TASK_TEAM

    workspace_root: str
    artifact_store_id: str | None = None
    checkpoint_namespace: str

    trace_id: str | None = None
    user_id: str | None = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ===== Workspace 路径辅助 =====

    def task_workspace(self, task_id: str) -> str:
        """返回 (并创建) `<workspace_root>/tasks/<task_id>` 目录绝对路径。

        task_id 中可能含 `/`（如 repair 节点 `A__repair_v2`）→ 不影响 os.path.join，
        但若有人传入 `..` 之类要规避，简单 replace 不允许分隔符。
        """
        safe_task = task_id.replace("/", "_").replace("\\", "_")
        path = os.path.join(self.workspace_root, "tasks", safe_task)
        os.makedirs(path, exist_ok=True)
        return path

    def task_relative_path(self, task_id: str, relative: str) -> str:
        """返回相对于 workspace_root 的 POSIX 风格路径字符串。

        用于 ArtifactStore.create(relative_path=...) 等需要相对路径的接口。
        """
        safe_task = task_id.replace("/", "_").replace("\\", "_")
        # relative 自身可能含反斜杠（Windows），统一成 POSIX
        safe_relative = relative.replace("\\", "/")
        return f"tasks/{safe_task}/{safe_relative}"

    def shared_dir(self) -> str:
        """返回 (并创建) `<workspace_root>/shared` 目录绝对路径。"""
        path = os.path.join(self.workspace_root, "shared")
        os.makedirs(path, exist_ok=True)
        return path

    def artifacts_dir(self) -> str:
        """返回 (并创建) `<workspace_root>/artifacts` 目录绝对路径。

        ArtifactStore 的 root_path 通常指向此目录。
        """
        path = os.path.join(self.workspace_root, "artifacts")
        os.makedirs(path, exist_ok=True)
        return path
