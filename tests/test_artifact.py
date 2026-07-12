"""Artifact 模型与 ArtifactStore 单元测试（§十）。"""
from __future__ import annotations

import os

import pytest

from app.multiagent.artifact import (
    Artifact,
    ArtifactRef,
    ArtifactStatus,
    ArtifactStore,
    ArtifactType,
    compute_content_hash,
    make_artifact_id,
    get_default_artifact_store,
    reset_default_artifact_store,
)


# ===== 哈希与 ID =====


def test_compute_content_hash_stable():
    h1 = compute_content_hash("hello")
    h2 = compute_content_hash("hello")
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_compute_content_hash_different_content():
    assert compute_content_hash("a") != compute_content_hash("b")


def test_compute_content_hash_bytes_input():
    h_str = compute_content_hash("hello")
    h_bytes = compute_content_hash(b"hello")
    assert h_str == h_bytes


def test_make_artifact_id_unique():
    ids = {make_artifact_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("art_") for i in ids)


# ===== Artifact 数据模型 =====


def test_artifact_to_dict_and_back():
    art = Artifact(
        id="art_x",
        run_id="run1",
        task_id="task1",
        type=ArtifactType.CODE,
        path="tasks/task1/main.py",
        content_hash="sha256:abc",
        size_bytes=100,
        version=1,
        produced_by="Coder",
    )
    d = art.to_dict()
    assert d["id"] == "art_x"
    assert d["type"] == "code"
    art2 = Artifact.from_dict(d)
    assert art2.id == art.id
    assert art2.type == ArtifactType.CODE
    assert art2.version == 1


def test_artifact_default_status_is_draft():
    art = Artifact(
        id="art_y", run_id="r", task_id="t",
        type=ArtifactType.CODE, path="p.py",
        content_hash="sha256:x", size_bytes=1, version=1, produced_by="A",
    )
    assert art.status == ArtifactStatus.DRAFT
    assert art.metadata == {}
    assert art.predecessor_id is None


def test_artifact_type_enum_values():
    assert ArtifactType.CODE == "code"
    assert ArtifactType.TEST == "test"
    assert ArtifactType.PATCH == "patch"
    assert ArtifactType.REPAIR_PATCH == "repair_patch"


def test_artifact_ref_from_artifact():
    art = Artifact(
        id="art_z", run_id="r", task_id="t",
        type=ArtifactType.CODE, path="p.py",
        content_hash="sha256:h", size_bytes=42, version=2,
        produced_by="Coder",
    )
    ref = ArtifactRef.from_artifact(art, summary="adds hello api")
    assert ref.artifact_id == "art_z"
    assert ref.version == 2
    assert ref.summary == "adds hello api"
    assert ref.content_hash == "sha256:h"


# ===== ArtifactStore 写入与查询 =====


def test_store_create_writes_to_disk(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    art = store.create(
        run_id="run1", task_id="task1", type="code",
        relative_path="tasks/task1/main.py",
        content="print('hello')",
        produced_by="Coder",
    )
    assert art.status == ArtifactStatus.PUBLISHED
    assert art.version == 1

    abs_path = os.path.join(str(tmp_path), "tasks/task1/main.py")
    assert os.path.isfile(abs_path)
    with open(abs_path, encoding="utf-8") as f:
        assert "print('hello')" in f.read()


def test_store_create_in_memory_mode_no_disk(tmp_path):
    """root_path=None 时不写磁盘，但注册表照常工作。"""
    store = ArtifactStore(root_path=None)
    art = store.create(
        run_id="r", task_id="t", type="code",
        relative_path="p.py", content="x = 1",
        produced_by="Coder",
    )
    assert art.status == ArtifactStatus.PUBLISHED
    assert store.read(art.id) is None  # 未启用磁盘


def test_store_compute_hash_matches_disk():
    store = ArtifactStore(root_path=None)
    content = "def add(a, b): return a + b"
    art = store.create(
        run_id="r", task_id="t", type="code",
        relative_path="p.py", content=content,
        produced_by="Coder",
    )
    assert art.content_hash == compute_content_hash(content)
    assert art.size_bytes == len(content.encode("utf-8"))


def test_store_list_by_task(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    store.create(
        run_id="r1", task_id="t1", type="code",
        relative_path="p1.py", content="x", produced_by="A",
    )
    store.create(
        run_id="r1", task_id="t2", type="code",
        relative_path="p2.py", content="y", produced_by="B",
    )
    assert len(store.list_by_task("t1")) == 1
    assert len(store.list_by_task("t2")) == 1
    assert len(store.list_by_task("missing")) == 0


def test_store_list_by_run_isolation():
    store = ArtifactStore(root_path=None)
    store.create(run_id="r1", task_id="t1", type="code",
                 relative_path="p.py", content="x", produced_by="A")
    store.create(run_id="r2", task_id="t1", type="code",
                 relative_path="p.py", content="y", produced_by="B")
    assert len(store.list_by_run("r1")) == 1
    assert len(store.list_by_run("r2")) == 1


def test_store_list_by_type(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    store.create(run_id="r1", task_id="t1", type="code",
                 relative_path="p.py", content="x", produced_by="A")
    store.create(run_id="r1", task_id="t2", type="test",
                 relative_path="t.py", content="y", produced_by="A")
    code_arts = store.list_by_type("r1", ArtifactType.CODE)
    assert len(code_arts) == 1
    test_arts = store.list_by_type("r1", "test")
    assert len(test_arts) == 1


def test_store_latest_for_task_returns_max_version():
    store = ArtifactStore(root_path=None)
    a1 = store.create(run_id="r", task_id="t1", type="code",
                      relative_path="p.py", content="v1", produced_by="A")
    a2 = store.create(run_id="r", task_id="t1", type="code",
                      relative_path="p.py", content="v2", produced_by="A",
                      parent_artifact_id=a1.id)  # 修复 +1 版本
    latest = store.latest_for_task("t1", "code")
    assert latest is not None
    assert latest.version == 2


def test_store_read_returns_content(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    art = store.create(
        run_id="r", task_id="t", type="code",
        relative_path="p.py", content="x = 1", produced_by="A",
    )
    content = store.read(art.id)
    assert content == "x = 1"


def test_store_read_missing_returns_none(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    assert store.read("nonexistent") is None


def test_store_get_returns_artifact(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    art = store.create(
        run_id="r", task_id="t", type="code",
        relative_path="p.py", content="x=1", produced_by="A",
    )
    fetched = store.get(art.id)
    assert fetched is not None
    assert fetched.id == art.id
    assert store.get("nonexistent") is None


# ===== 版本管理 =====


def test_create_repair_increments_version_and_supersedes_parent(tmp_path):
    """修复产物生成新版本，原版本标 SUPERSEDED。"""
    store = ArtifactStore(root_path=str(tmp_path))
    v1 = store.create(
        run_id="r", task_id="t1", type="code",
        relative_path="p.py", content="def f(): pass",
        produced_by="Coder",
    )
    assert v1.version == 1
    assert v1.status == ArtifactStatus.PUBLISHED

    # 修复
    v2 = store.create(
        run_id="r", task_id="t1_repair", type="repair_patch",
        relative_path="repairs/t1_repair/p.py",
        content="def f(): return 42",
        produced_by="Coder",
        parent_artifact_id=v1.id,
    )
    assert v2.version == 2
    assert v2.parent_artifact_id == v1.id
    assert v2.predecessor_id == v1.id

    # 原版本被取代
    updated_v1 = store.get(v1.id)
    assert updated_v1.status == ArtifactStatus.SUPERSEDED


def test_latest_for_task_none_when_empty():
    store = ArtifactStore(root_path=None)
    assert store.latest_for_task("t_missing") is None


def test_chain_of_repairs_keeps_version_increasing(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    a1 = store.create(run_id="r", task_id="t", type="code",
                      relative_path="p.py", content="v1", produced_by="A")
    a2 = store.create(run_id="r", task_id="t", type="code",
                      relative_path="p.py", content="v2", produced_by="A",
                      parent_artifact_id=a1.id)
    a3 = store.create(run_id="r", task_id="t", type="code",
                      relative_path="p.py", content="v3", produced_by="A",
                      parent_artifact_id=a2.id)
    assert a2.version == 2
    assert a3.version == 3
    # 验证链
    assert a3.predecessor_id == a2.id
    assert a2.predecessor_id == a1.id
    # 旧版本都 SUPERSEDED
    assert store.get(a1.id).status == ArtifactStatus.SUPERSEDED
    assert store.get(a2.id).status == ArtifactStatus.SUPERSEDED


# ===== 状态变更 =====


def test_mark_verified(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    art = store.create(run_id="r", task_id="t", type="code",
                       relative_path="p.py", content="x", produced_by="A")
    assert store.mark_verified(art.id)
    assert store.get(art.id).status == ArtifactStatus.VERIFIED


def test_mark_rejected(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    art = store.create(run_id="r", task_id="t", type="code",
                       relative_path="p.py", content="x", produced_by="A")
    assert store.mark_rejected(art.id)
    assert store.get(art.id).status == ArtifactStatus.REJECTED


def test_supersede(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    art = store.create(run_id="r", task_id="t", type="code",
                       relative_path="p.py", content="x", produced_by="A")
    assert store.supersede(art.id)
    assert store.get(art.id).status == ArtifactStatus.SUPERSEDED


def test_mark_verified_missing_returns_false():
    store = ArtifactStore(root_path=None)
    assert store.mark_verified("nonexistent") is False


# ===== Integrity =====


def test_verify_integrity_matches_disk(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    art = store.create(run_id="r", task_id="t", type="code",
                       relative_path="p.py", content="x = 1", produced_by="A")
    assert store.verify_integrity(art.id) is True


def test_verify_integrity_detects_tamper(tmp_path):
    """外部篡改文件，verify_integrity 应失败。"""
    store = ArtifactStore(root_path=str(tmp_path))
    art = store.create(run_id="r", task_id="t", type="code",
                       relative_path="p.py", content="x = 1", produced_by="A")
    # 篡改
    abs_path = os.path.join(str(tmp_path), art.path)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write("MALICIOUS")
    assert store.verify_integrity(art.id) is False


# ===== 跨 Run 隔离 =====


def test_is_in_run_isolation(tmp_path):
    store = ArtifactStore(root_path=str(tmp_path))
    a = store.create(run_id="run_a", task_id="t", type="code",
                     relative_path="p.py", content="x", produced_by="A")
    assert store.is_in_run(a.id, "run_a")
    assert not store.is_in_run(a.id, "run_b")
    assert not store.is_in_run("nonexistent", "run_a")


# ===== 默认 store 单例 =====


def test_default_store_singleton():
    reset_default_artifact_store()
    s1 = get_default_artifact_store()
    s2 = get_default_artifact_store()
    assert s1 is s2
    reset_default_artifact_store()
