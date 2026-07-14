"""Run-level workspace isolation 单元测试（§九）。"""
from __future__ import annotations

import os

import pytest

from app.multiagent.run_workspace import (
    RunWorkspace,
    check_write_permission,
    create_run_workspace,
    get_run_workspace,
    is_within_run,
    remove_run_workspace,
    reset_workspaces,
)


# ===== RunWorkspace 基础 =====


def test_workspace_creates_dirs(tmp_path):
    ws = RunWorkspace(run_id="run1", base_root=str(tmp_path))
    assert ws.exists()
    assert os.path.isdir(ws.workspace_root)
    assert os.path.isdir(ws.shared_dir)
    assert os.path.isdir(ws.tasks_dir)
    assert os.path.isdir(ws.artifacts_dir)
    assert os.path.isdir(ws.checkpoints_dir)


def test_workspace_with_create_dirs_false(tmp_path):
    ws = RunWorkspace(run_id="run2", base_root=str(tmp_path), create_dirs=False)
    assert not ws.exists()  # 未创建


def test_task_dir_auto_creates(tmp_path):
    ws = RunWorkspace(run_id="run1", base_root=str(tmp_path))
    td = ws.task_dir("task1")
    assert os.path.isdir(td)
    assert td == os.path.join(ws.tasks_dir, "task1")


def test_artifacts_root(tmp_path):
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    assert ws.artifacts_root() == ws.artifacts_dir


def test_checkpoint_path_default(tmp_path):
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    cp = ws.checkpoint_path()
    assert cp.endswith("checkpoints" + os.sep + "team.sqlite3")


def test_checkpoint_path_named(tmp_path):
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    cp = ws.checkpoint_path("custom.db")
    assert cp.endswith(os.sep + "custom.db")


# ===== Cleanup =====


def test_cleanup_removes_root(tmp_path):
    ws = RunWorkspace(run_id="r_clean", base_root=str(tmp_path))
    ws.task_dir("t1")
    assert ws.exists()
    ws.cleanup()
    assert not ws.exists()


def test_cleanup_idempotent(tmp_path):
    ws = RunWorkspace(run_id="r_idem", base_root=str(tmp_path))
    ws.cleanup()
    ws.cleanup()  # 应不报错


def test_size_bytes_empty_after_create(tmp_path):
    ws = RunWorkspace(run_id="r_size", base_root=str(tmp_path))
    # 创建几个空目录后 size 仍约为 0
    assert ws.size_bytes() == 0


def test_size_bytes_includes_files(tmp_path):
    ws = RunWorkspace(run_id="r_files", base_root=str(tmp_path))
    with open(os.path.join(ws.task_dir("t1"), "a.txt"), "w") as f:
        f.write("hello")
    assert ws.size_bytes() >= 5


# ===== is_within_run =====


def test_is_within_run_inside(tmp_path):
    ws = RunWorkspace(run_id="r_in", base_root=str(tmp_path))
    p = os.path.join(ws.workspace_root, "tasks", "t1", "file.py")
    assert is_within_run(p, ws)


def test_is_within_run_exact_root(tmp_path):
    ws = RunWorkspace(run_id="r_eq", base_root=str(tmp_path))
    assert is_within_run(ws.workspace_root, ws)


def test_is_within_run_outside(tmp_path):
    ws = RunWorkspace(run_id="r_out", base_root=str(tmp_path))
    sibling = os.path.abspath(os.path.join(ws.workspace_root, "..", "other"))
    assert not is_within_run(sibling, ws)


def test_is_within_run_rejects_dotdot(tmp_path):
    ws = RunWorkspace(run_id="r_dd", base_root=str(tmp_path))
    # workspace_root/tasks/t1/../../other_run/data 走出 root
    abs_path = os.path.abspath(
        os.path.join(ws.workspace_root, "tasks", "..", "..", "sneaky.txt")
    )
    assert not is_within_run(abs_path, ws)


def test_is_within_run_rejects_prefix_attack(tmp_path):
    """run-A/tasks/... 不属于 run-A-evil 的 workspace。"""
    ws_a = RunWorkspace(run_id="A", base_root=str(tmp_path))
    ws_evil = RunWorkspace(run_id="A-evil", base_root=str(tmp_path))
    abs_path = os.path.join(ws_a.workspace_root, "shared", "secret.txt")
    assert not is_within_run(abs_path, ws_evil)


def test_is_within_run_rejects_symlink_escape(tmp_path):
    """A path lexically under the run may still resolve outside through a symlink."""
    ws = RunWorkspace(run_id="r_link", base_root=str(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, os.path.join(ws.tasks_dir, "escaped"))
    assert not is_within_run(os.path.join(ws.tasks_dir, "escaped", "owned.py"), ws)


def test_task_dir_rejects_traversal_task_id(tmp_path):
    ws = RunWorkspace(run_id="r_task_id", base_root=str(tmp_path))
    with pytest.raises(ValueError, match="escapes"):
        ws.task_dir("../outside")


# ===== check_write_permission =====


def test_check_write_task_dir_permitted_when_worker_in_assigned_task(tmp_path):
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    target = os.path.join(ws.task_dir("t1"), "main.py")
    ok, reason = check_write_permission(target, ws, agent_role="Coder", task_id="t1")
    assert ok, reason
    assert reason is None


def test_check_write_task_dir_denied_for_other_task(tmp_path):
    """Coder 被分配 t1，不能写 t2 的目录。"""
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    ws.task_dir("t1")
    target_other = os.path.join(ws.tasks_dir, "t2", "x.py")
    os.makedirs(os.path.dirname(target_other), exist_ok=True)
    ok, reason = check_write_permission(target_other, ws, agent_role="Coder", task_id="t1")
    assert not ok
    assert "cannot write to tasks/t2" in reason


def test_check_write_shared_permitted_for_finalizer(tmp_path):
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    target = os.path.join(ws.shared_dir, "result.md")
    ok, reason = check_write_permission(target, ws, agent_role="Finalizer")
    assert ok, reason is None


def test_check_write_shared_denied_for_coder(tmp_path):
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    target = os.path.join(ws.shared_dir, "x.md")
    ok, reason = check_write_permission(target, ws, agent_role="Coder")
    assert not ok
    assert "shared requires" in reason


def test_check_write_outside_run_denied(tmp_path):
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    target = str(tmp_path / "outside.txt")
    ok, reason = check_write_permission(target, ws, agent_role="Coder")
    assert not ok
    assert "outside run" in reason


def test_check_write_artifacts_dir_denied_for_worker(tmp_path):
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    target = os.path.join(ws.artifacts_dir, "x.py")
    ok, reason = check_write_permission(target, ws, agent_role="Coder")
    assert not ok
    assert "artifacts" in reason.lower()


def test_check_write_checkpoints_dir_denied(tmp_path):
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    target = os.path.join(ws.checkpoints_dir, "evil.db")
    ok, reason = check_write_permission(target, ws, agent_role="Coder")
    assert not ok
    assert "checkpoints" in reason.lower()


def test_check_write_shared_permitted_for_planner(tmp_path):
    ws = RunWorkspace(run_id="r1", base_root=str(tmp_path))
    target = os.path.join(ws.shared_dir, "plan.md")
    ok, reason = check_write_permission(target, ws, agent_role="Planner")
    assert ok


# ===== Workspace manager =====


def test_create_and_get_workspace(tmp_path):
    reset_workspaces()
    ws = create_run_workspace("m1", base_root=str(tmp_path))
    assert get_run_workspace("m1") is ws


def test_remove_workspace(tmp_path):
    reset_workspaces()
    create_run_workspace("m2", base_root=str(tmp_path))
    remove_run_workspace("m2", cleanup=True)
    assert get_run_workspace("m2") is None
