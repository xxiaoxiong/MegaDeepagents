"""Artifact：真实产物模型（docs/upgradePhaseTwo.md §十）。

设计目标：
1. Agent 之间不再通过超长消息传递代码和报告，而是通过 Artifact ID 引用
2. Artifact 内容真实存在于磁盘上（写入 Workspace），不再只是 prompt 字符串
3. 计算内容哈希支持幂等检测与版本对比
4. Verifier 直接读取 Artifact 内容，不依赖 Agent 转述
5. 修复产物生成新版本，不覆盖旧版本——保留审计链

每个 Artifact 的归属：
- run_id：所属 Run（对应 §九 Run 级 workspace）
- task_id：产出它的 TaskNode
- produced_by：执行 Agent 名（用于审计）
- content_hash + version：唯一性
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.core.logging import logger


class ArtifactType(str, Enum):
    """Artifact 内容类型（与 OutputContract.artifact_type 对齐）。"""

    CODE = "code"
    TEST = "test"
    PATCH = "patch"
    DOCUMENT = "document"
    REPORT = "report"
    DATA = "data"
    PLAN = "plan"
    CONFIG = "config"
    REVIEW = "review"
    REPAIR_PATCH = "repair_patch"
    ANY = "any"


class ArtifactStatus(str, Enum):
    """Artifact 状态。"""

    DRAFT = "draft"               # Agent 写中（写入完成前）
    PUBLISHED = "published"        # 已发布到存储，可被下游消费
    VERIFIED = "verified"          # Verifier PASS 后的版本
    REJECTED = "rejected"          # Verifier/Reviewer 拒绝
    SUPERSEDED = "superseded"      # 被新版本取代（保留为审计历史）


@dataclass
class Artifact:
    """真实产物实体。

    所有字段都是审计可追溯信息。实际内容存于 `path` 指向的磁盘文件。
    """

    id: str
    run_id: str
    task_id: str
    type: ArtifactType
    path: str                       # 磁盘相对路径（相对于 run workspace root）
    content_hash: str
    size_bytes: int
    version: int                     # 版本号，从 1 开始；fix 后产生新 version
    produced_by: str                # Agent 名
    status: ArtifactStatus = ArtifactStatus.DRAFT
    created_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)
    predecessor_id: str | None = None  # 上一版本（修复链）
    parent_artifact_id: str | None = None  # 修复对象（repair patch 对应的原 artifact）

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "type": self.type.value,
            "path": self.path,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "version": self.version,
            "produced_by": self.produced_by,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
            "predecessor_id": self.predecessor_id,
            "parent_artifact_id": self.parent_artifact_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Artifact":
        return cls(
            id=d["id"],
            run_id=d["run_id"],
            task_id=d["task_id"],
            type=ArtifactType(d.get("type", "any")),
            path=d["path"],
            content_hash=d["content_hash"],
            size_bytes=int(d["size_bytes"]),
            version=int(d.get("version", 1)),
            produced_by=d["produced_by"],
            status=ArtifactStatus(d.get("status", "draft")),
            created_at=datetime.fromisoformat(d["created_at"]) if d.get("created_at") else datetime.utcnow(),
            metadata=dict(d.get("metadata", {})),
            predecessor_id=d.get("predecessor_id"),
            parent_artifact_id=d.get("parent_artifact_id"),
        )


@dataclass
class ArtifactRef:
    """Agent 之间消息传递用的引用（避免长内容传输）。"""

    artifact_id: str
    type: ArtifactType
    path: str
    content_hash: str
    version: int
    summary: str = ""                    # 关键证据摘要
    evidence_excerpts: list[str] = field(default_factory=list)

    @classmethod
    def from_artifact(cls, art: Artifact, summary: str = "") -> "ArtifactRef":
        return cls(
            artifact_id=art.id,
            type=art.type,
            path=art.path,
            content_hash=art.content_hash,
            version=art.version,
            summary=summary,
        )


def compute_content_hash(content: str | bytes) -> str:
    """SHA-256 内容哈希（前 16 位 hex 用于可读性）。"""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def make_artifact_id() -> str:
    """生成唯一 Artifact ID。

    不使用时间戳/随机数（避免在 checkpoint 重放时不可重入）。
    用 monotonic 计数器 + 进程内 state 模拟，但因 artifact 创建是
    业务事件（仅真正写入文件时一次），所以保留 uuid 模拟语义。
    """
    import uuid
    return "art_" + uuid.uuid4().hex[:16]


# ===== Artifact Store：磁盘 + 注册表 =====


class ArtifactStore:
    """Artifact 存储和注册表。

    职责：
    1. 物理文件写入：write(artifact, content) → 写入磁盘 + 计算 hash
    2. 注册表查询：by_id / by_task / by_run / by_type
    3. 版本管理：修复产生新版本，旧版本标 SUPERSEDED
    4. 内容读取：read(artifact_id) → 文件内容
    5. 跨 Run 不可访问（root_path 由 run_id 隔离）

    Phase G #14: create() 时同步写到 SQLite（phase_g_store.insert_artifact），
    允许跨进程重启后通过 load_from_db(run_id) 重建内存注册表。
    """

    def __init__(self, root_path: str | None = None) -> None:
        # root_path = run workspace 根目录；None 表示禁用磁盘写入
        self._root_path = root_path
        self._registry: dict[str, Artifact] = {}      # id → Artifact
        self._by_task: dict[str, list[str]] = {}      # task_id → [artifact_id]
        self._by_run: dict[str, list[str]] = {}       # run_id → [artifact_id]

    # ---- 写入 ----
    def create(
        self,
        *,
        run_id: str,
        task_id: str,
        type: ArtifactType | str,
        relative_path: str,           # 相对于 run_root 的路径
        content: str | bytes,
        produced_by: str,
        parent_artifact_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        """创建并写入新 Artifact。返回 published Artifact。

        流程：
        1. 计算 hash、size
        2. 写入磁盘（如启用 root_path）
        3. 注册到内存索引
        4. 同步写入 SQLite（Phase G: persistence）
        5. 标记为 PUBLISHED
        """
        artifact_type = type if isinstance(type, ArtifactType) else ArtifactType(type)
        content_hash = compute_content_hash(content)
        size = len(content.encode("utf-8") if isinstance(content, str) else content)

        # 确定版本：若存在 parent_artifact_id（修复场景），版本 = parent.version + 1
        version = 1
        predecessor_id = None
        if parent_artifact_id and parent_artifact_id in self._registry:
            parent = self._registry[parent_artifact_id]
            version = parent.version + 1
            predecessor_id = parent.id
            # parent 标 SUPERSEDED（保留以审计，不再消费）
            parent.status = ArtifactStatus.SUPERSEDED

        artifact_id = make_artifact_id()
        artifact = Artifact(
            id=artifact_id,
            run_id=run_id,
            task_id=task_id,
            type=artifact_type,
            path=relative_path,
            content_hash=content_hash,
            size_bytes=size,
            version=version,
            produced_by=produced_by,
            status=ArtifactStatus.PUBLISHED,
            metadata=metadata or {},
            predecessor_id=predecessor_id,
            parent_artifact_id=parent_artifact_id,
        )

        # 写入磁盘
        self._write_to_disk(artifact, content)

        # 注册到内存
        self._registry[artifact.id] = artifact
        self._by_task.setdefault(task_id, []).append(artifact.id)
        self._by_run.setdefault(run_id, []).append(artifact.id)

        # Phase G: 同步写入 SQLite（跨进程恢复）
        try:
            from app.multiagent.phase_g_store import get_agent_run_history
            from app.multiagent.store import _get_conn
            # 确保 sqlite 连接存在可写
            h = get_agent_run_history()
            h.insert_artifact(
                artifact_id=artifact.id,
                run_id=run_id,
                task_id=task_id,
                type=artifact_type.value,
                relative_path=relative_path,
                content_hash=content_hash,
                size_bytes=size,
                version=version,
                produced_by=produced_by,
                status="published",
                predecessor_id=predecessor_id,
                parent_artifact_id=parent_artifact_id,
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning(f"[ArtifactStore] SQLite persist failed for {artifact.id}: {exc}")

        logger.debug(
            f"[ArtifactStore] create id={artifact.id} type={artifact_type.value} "
            f"version={version} hash={content_hash[:24]} bytes={size}"
        )
        return artifact

    # ---- 从 SQLite 恢复 ----

    def load_from_db(self, run_id: str) -> int:
        """从 SQLite phase_g_store 重建本 run 的所有 Artifact 内存注册表。

        跨进程重启后调用，确保 resume 后可查询到之前的 Artifact 记录。
        返回恢复条数。幂等：已存在同 artifact_id 的内存条目跳过。
        """
        try:
            from app.multiagent.phase_g_store import get_agent_run_history
            h = get_agent_run_history()
            rows = h.list_artifacts_by_run(run_id)
        except Exception as exc:
            logger.warning(f"[ArtifactStore] load_from_db run={run_id} 失败: {exc}")
            return 0

        count = 0
        for r in rows:
            aid = r.get("artifact_id")
            if not aid or aid in self._registry:
                continue
            try:
                from datetime import datetime
                artifact = Artifact(
                    id=aid,
                    run_id=r.get("run_id", run_id),
                    task_id=r.get("task_id", ""),
                    type=ArtifactType(r.get("type", "any")),
                    path=r.get("relative_path", ""),
                    content_hash=r.get("content_hash", ""),
                    size_bytes=int(r.get("size_bytes", 0)),
                    version=int(r.get("version", 1)),
                    produced_by=r.get("produced_by", ""),
                    status=ArtifactStatus(r.get("status", "published")),
                    created_at=datetime.fromisoformat(r["created_at"]) if isinstance(r.get("created_at"), str) else datetime.utcnow(),
                    metadata=dict(r.get("metadata", {}) if isinstance(r.get("metadata"), dict) else {}),
                    predecessor_id=r.get("predecessor_id"),
                    parent_artifact_id=r.get("parent_artifact_id"),
                )
                self._registry[aid] = artifact
                task_id = r.get("task_id", "")
                self._by_task.setdefault(task_id, []).append(aid)
                self._by_run.setdefault(run_id, []).append(aid)
                count += 1
            except Exception as exc:
                logger.warning(f"[ArtifactStore] load_from_db row {aid} 跳过: {exc}")
        logger.info(f"[ArtifactStore] load_from_db run={run_id} 恢复 {count} 个 Artifact")
        return count

    def _write_to_disk(self, artifact: Artifact, content: str | bytes) -> None:
        """实际写入磁盘；root_path 为 None 时跳过（仅 in-memory）。"""
        if not self._root_path:
            # 仅内存模式（测试或 diskless 场景）
            return
        abs_path = os.path.join(self._root_path, artifact.path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        if isinstance(content, str):
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            with open(abs_path, "wb") as f:
                f.write(content)

    # ---- 读取 ----
    def read(self, artifact_id: str) -> str | None:
        """读 Artifact 内容；root_path 未启用或文件不存在则返回 None。"""
        artifact = self._registry.get(artifact_id)
        if artifact is None:
            return None
        if not self._root_path:
            return None
        abs_path = os.path.join(self._root_path, artifact.path)
        if not os.path.isfile(abs_path):
            return None
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()

    def get(self, artifact_id: str) -> Artifact | None:
        return self._registry.get(artifact_id)

    def list_by_task(self, task_id: str) -> list[Artifact]:
        ids = self._by_task.get(task_id, [])
        return [self._registry[i] for i in ids if i in self._registry]

    def list_by_run(self, run_id: str) -> list[Artifact]:
        ids = self._by_run.get(run_id, [])
        return [self._registry[i] for i in ids if i in self._registry]

    def list_by_type(self, run_id: str, type: ArtifactType | str) -> list[Artifact]:
        artifact_type = type if isinstance(type, ArtifactType) else ArtifactType(type)
        return [
            a for a in self.list_by_run(run_id)
            if a.type == artifact_type
        ]

    def latest_for_task(self, task_id: str, type: ArtifactType | str | None = None) -> Artifact | None:
        """某 task 的最新版本（type 可选过滤）。"""
        arts = self.list_by_task(task_id)
        if type is not None:
            artifact_type = type if isinstance(type, ArtifactType) else ArtifactType(type)
            arts = [a for a in arts if a.type == artifact_type]
        if not arts:
            return None
        return max(arts, key=lambda a: a.version)

    def verify_integrity(self, artifact_id: str) -> bool:
        """重读磁盘计算 hash，比对注册表中的 hash。"""
        artifact = self._registry.get(artifact_id)
        if artifact is None:
            return False
        content = self.read(artifact_id)
        if content is None:
            return False
        return compute_content_hash(content) == artifact.content_hash

    # ---- 状态变更 ----
    def mark_verified(self, artifact_id: str) -> bool:
        a = self._registry.get(artifact_id)
        if a is None:
            return False
        a.status = ArtifactStatus.VERIFIED
        return True

    def mark_rejected(self, artifact_id: str) -> bool:
        a = self._registry.get(artifact_id)
        if a is None:
            return False
        a.status = ArtifactStatus.REJECTED
        return True

    def supersede(self, artifact_id: str) -> bool:
        a = self._registry.get(artifact_id)
        if a is None:
            return False
        a.status = ArtifactStatus.SUPERSEDED
        return True

    # ---- 跨 Run 隔离 ----
    def is_in_run(self, artifact_id: str, run_id: str) -> bool:
        a = self._registry.get(artifact_id)
        return a is not None and a.run_id == run_id

    @property
    def root_path(self) -> str | None:
        return self._root_path


# ===== 全局单例（Run 级隔离由 caller 在创建时按 run 维护独立 store） =====


_default_store: ArtifactStore | None = None


def get_default_artifact_store(root_path: str | None = None) -> ArtifactStore:
    """获取默认 store。生产中每个 Run 应构造独立 store（root_path 经 §九 隔离）。"""
    global _default_store
    if _default_store is None:
        _default_store = ArtifactStore(root_path=root_path)
    return _default_store


def reset_default_artifact_store() -> None:
    """重置全局 store（测试用）。"""
    global _default_store
    _default_store = None
