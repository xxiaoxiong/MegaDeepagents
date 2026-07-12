"""Run-level workspace isolation（docs/upgradePhaseTwo.md §九）。

设计：
- 每个 Run 拥有独立 workspace 根目录
- 子 Agent 只能写入自己的 task 子目录
- 跨 Run 隔离：通过 root_path 物理隔离
- 全局共享只读区（shared）用于跨 Run 协作

布局示例：
    <root>/run-001/
        shared/         # Run 内共享只读 + Finalizer 可写
        tasks/
            task1/       # Coder 写入
            task2/       # Tester 写入
            ...
        artifacts/      # ArtifactStore 的 root（共享只读路径解析）
        checkpoints/
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from app.core.logging import logger


class RunWorkspace:
    """Run 级工作空间。

    提供：
    - workspace_root: Run 根目录
    - task_dir(task_id): 返回该 task 的子目录并自动创建
    - shared_dir: Run 内共享目录
    - artifacts_dir: ArtifactStore 的根（与 §十 Artifact 模块对接）
    - checkpoints_dir: LangGraph checkpoint sqlite 文件统一存放
    """

    def __init__(self, run_id: str, base_root: str | None = None, create_dirs: bool = True) -> None:
        """
        Args:
            run_id: Run 唯一 ID
            base_root: 所有 Run 之上的根目录；None 使用系统 temp
            create_dirs: 是否立即创建目录结构（测试时可推迟）
        """
        self.run_id = run_id
        self._base_root = base_root or tempfile.gettempdir()

        # Run 子目录名带上 run_id 前缀以便文件系统观察
        self.workspace_root = os.path.join(self._base_root, f"run-{run_id}")
        self.shared_dir = os.path.join(self.workspace_root, "shared")
        self.tasks_dir = os.path.join(self.workspace_root, "tasks")
        self.artifacts_dir = os.path.join(self.workspace_root, "artifacts")
        self.checkpoints_dir = os.path.join(self.workspace_root, "checkpoints")

        if create_dirs:
            self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for d in [self.workspace_root, self.shared_dir, self.tasks_dir,
                  self.artifacts_dir, self.checkpoints_dir]:
            os.makedirs(d, exist_ok=True)

    # ===== Tasks =====

    def task_dir(self, task_id: str) -> str:
        """返回 task 子目录（自动创建）。"""
        path = os.path.join(self.tasks_dir, task_id)
        os.makedirs(path, exist_ok=True)
        return path

    def task_relative_path(self, task_id: str, relative: str) -> str:
        """构 task 文件的相对路径（用于 ArtifactStore）。"""
        safe_task = task_id.replace("/", "_").replace("\\", "_")
        return f"tasks/{safe_task}/{relative}"

    def task_relative_to_run(self, abs_path: str) -> str:
        """返回从 workspace_root 算起的相对路径。"""
        return os.path.relpath(abs_path, self.workspace_root)

    # ===== Shared =====

    def shared_read_dir(self) -> str:
        return self.shared_dir

    def can_write_to_shared(self, agent_role: str) -> bool:
        """仅 Finalizer 可以写 shared 区。"""
        return agent_role.lower() in {"finalizer", "planner"}

    # ===== Artifacts integration =====

    def artifacts_root(self) -> str:
        """ArtifactStore 的 root_path（与 §十 对接）。"""
        return self.artifacts_dir

    # ===== Checkpoints =====

    def checkpoint_path(self, name: str = "team.sqlite3") -> str:
        return os.path.join(self.checkpoints_dir, name)

    # ===== Cleanup =====

    def cleanup(self) -> None:
        """删除整个 Run 目录。"""
        if os.path.isdir(self.workspace_root):
            shutil.rmtree(self.workspace_root, ignore_errors=True)
            logger.info(f"[RunWorkspace] cleanup: {self.workspace_root}")

    def exists(self) -> bool:
        return os.path.isdir(self.workspace_root)

    def size_bytes(self) -> int:
        if not self.exists():
            return 0
        total = 0
        for root, _, files in os.walk(self.workspace_root):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return total

    def __repr__(self) -> str:
        return f"RunWorkspace(run_id={self.run_id!r}, root={self.workspace_root!r})"


# ===== 隔离检查：路径越权检测 =====


def is_within_run(abs_path: str, run_workspace: RunWorkspace) -> bool:
    """检查 abs_path 是否在 Run workspace 之内（防止 ../ 越权）。"""
    abs_path = os.path.abspath(abs_path)
    root = os.path.abspath(run_workspace.workspace_root)
    return abs_path == root or abs_path.startswith(root + os.sep)


def check_write_permission(
    abs_path: str,
    run_workspace: RunWorkspace,
    agent_role: str,
    task_id: str | None = None,
) -> tuple[bool, str | None]:
    """检查 agent 是否有权写入指定路径。

    规则：
    1. 路径必须在 Run workspace 之内
    2. 写入 task 子目录：仅当 task_id 匹配该 agent 被分配的 task
       （此处简化：调用方传入 task_id 即视为有权限）
    3. 写入 shared：仅 Finalizer / Planner
    4. 写入 artifacts：仅 ArtifactStore 内部接口（不开放给 worker）
    """
    abs_path = os.path.abspath(abs_path)
    if not is_within_run(abs_path, run_workspace):
        return False, "path outside run workspace"

    root = os.path.abspath(run_workspace.workspace_root)
    rel = os.path.relpath(abs_path, root)
    parts = rel.split(os.sep) if rel != "." else []

    # 检查 shared 区写入
    if parts and parts[0] == "shared":
        if run_workspace.can_write_to_shared(agent_role):
            return True, None
        return False, "shared requires finalizer/planner role"

    # 检查 tasks/<task_id>/ 写入
    if len(parts) >= 2 and parts[0] == "tasks":
        # 若调用方传入了 task_id，要求一致；否则允许（宽松检查）
        if task_id and parts[1] != task_id:
            return False, f"task {task_id} cannot write to tasks/{parts[1]}"
        return True, None

    # 检查 artifacts 区写入：禁止 worker 直接写
    if parts and parts[0] == "artifacts":
        return False, "artifacts dir is managed by ArtifactStore"

    # checkpoints 禁止 worker 直接写
    if parts and parts[0] == "checkpoints":
        return False, "checkpoints dir is system-managed"

    return False, "unknown writable region"


# ===== 全局管理 =====


_active_workspaces: dict[str, RunWorkspace] = {}


def create_run_workspace(run_id: str, base_root: str | None = None) -> RunWorkspace:
    """创建一个 Run workspace 并记录到全局表中。"""
    ws = RunWorkspace(run_id=run_id, base_root=base_root)
    _active_workspaces[run_id] = ws
    return ws


def get_run_workspace(run_id: str) -> RunWorkspace | None:
    return _active_workspaces.get(run_id)


def remove_run_workspace(run_id: str, cleanup: bool = True) -> None:
    ws = _active_workspaces.pop(run_id, None)
    if ws and cleanup:
        ws.cleanup()


def reset_workspaces() -> None:
    """测试隔离用：清空全局表。"""
    _active_workspaces.clear()
