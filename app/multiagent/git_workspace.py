"""Git worktree isolation, leases and governed integration for coding teammates."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from app.multiagent.permission import PermissionBroker, PermissionKind
from app.multiagent.shell_policy import ShellCommandRunner
from app.multiagent.store import _get_conn


def _git(repo: str | Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), *argv], shell=False,
                            capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:80] or "agent"


class WorkspaceProvider(Protocol):
    def workspace_for(self, agent_id: str) -> str: ...


class LocalWorkspaceProvider:
    """Explicit non-Git provider for non-code runs; never selected implicitly."""
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def workspace_for(self, agent_id: str) -> str:
        path = self.root / "workspaces" / _slug(agent_id)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


@dataclass
class WorktreeLease:
    lease_id: str
    run_id: str
    agent_id: str
    worktree_path: str
    branch: str
    acquired_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime = field(default_factory=lambda: datetime.utcnow() + timedelta(minutes=10))
    released_at: datetime | None = None

    def active(self) -> bool:
        return self.released_at is None and self.expires_at > datetime.utcnow()


def _ensure_schema() -> None:
    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS worktree_leases (
            lease_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            worktree_path TEXT NOT NULL UNIQUE, branch TEXT NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL, acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, released_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_worktree_leases_agent
            ON worktree_leases(run_id, agent_id, status);
        CREATE TABLE IF NOT EXISTS merge_queue (
            queue_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            commit_sha TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


class RepositoryWorkspaceManager:
    def __init__(
        self, source_repository: str, run_workspace: str,
        *, base_branch: str | None = None, base_commit_sha: str | None = None,
    ) -> None:
        self.source_repository = str(Path(source_repository).resolve())
        self.run_workspace = str(Path(run_workspace).resolve())
        if _git(self.source_repository, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            raise ValueError("source_repository is not a Git worktree")
        self.base_branch = base_branch or self._default_branch()
        self.base_commit_sha = base_commit_sha or _git(
            self.source_repository, "rev-parse", self.base_branch
        ).stdout.strip()
        root = Path(self.run_workspace)
        for name in ("control", "artifacts", "worktrees", "integration", "logs"):
            (root / name).mkdir(parents=True, exist_ok=True)
        manifest = {
            "source_repository": self.source_repository,
            "base_branch": self.base_branch, "base_commit_sha": self.base_commit_sha,
            "created_at": datetime.utcnow().isoformat(),
        }
        (root / "control" / "repository.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    def _default_branch(self) -> str:
        symbolic = _git(self.source_repository, "symbolic-ref", "--short", "refs/remotes/origin/HEAD",
                        check=False)
        if symbolic.returncode == 0 and symbolic.stdout.strip():
            return symbolic.stdout.strip().removeprefix("origin/")
        current = _git(self.source_repository, "branch", "--show-current").stdout.strip()
        return current or "HEAD"


class AgentWorktreeManager:
    def __init__(self, repository: RepositoryWorkspaceManager,
                 lease_seconds: int = 600,
                 permission_broker: PermissionBroker | None = None,
                 environment_file_allowlist: list[str] | None = None) -> None:
        self.repository = repository
        self.lease_seconds = lease_seconds
        self.permission_broker = permission_broker
        # Empty by default: a new worktree never inherits local environment or
        # credential files merely because they exist in the source checkout.
        self.environment_file_allowlist = tuple(environment_file_allowlist or ())
        self._lock = threading.RLock()
        _ensure_schema()

    def acquire(self, run_id: str, agent_id: str) -> WorktreeLease:
        with self._lock:
            existing = self.get(run_id, agent_id)
            if existing and existing.active() and Path(existing.worktree_path).is_dir():
                existing.expires_at = datetime.utcnow() + timedelta(seconds=self.lease_seconds)
                self._save(existing, "active")
                return existing
            path = Path(self.repository.run_workspace) / "worktrees" / _slug(agent_id)
            branch = f"agent/{_slug(run_id)}/{_slug(agent_id)}"
            if self.permission_broker is None:
                raise PermissionError("Git branch creation requires PermissionBroker")
            self.permission_broker.authorize(
                run_id=run_id, agent_id=agent_id, kind=PermissionKind.GIT_BRANCH,
                operation="git_worktree_branch",
                parameters={"branch": branch, "base_sha": self.repository.base_commit_sha,
                            "worktree": str(path)},
            )
            if path.exists():
                # A path not registered as this Agent's lease is never reused.
                raise RuntimeError(f"unleased worktree path already exists: {path}")
            result = _git(self.repository.source_repository, "worktree", "add", "-b", branch,
                          str(path), self.repository.base_commit_sha, check=False)
            if result.returncode != 0 and "already exists" in result.stderr:
                _git(self.repository.source_repository, "worktree", "add", str(path), branch)
            elif result.returncode != 0:
                raise RuntimeError(result.stderr.strip())
            self._copy_allowed_environment_files(path)
            lease = WorktreeLease(
                lease_id="lease_" + uuid.uuid4().hex[:16], run_id=run_id,
                agent_id=agent_id, worktree_path=str(path), branch=branch,
                expires_at=datetime.utcnow() + timedelta(seconds=self.lease_seconds),
            )
            self._save(lease, "active")
            return lease

    def _copy_allowed_environment_files(self, worktree: Path) -> None:
        """Copy only explicitly allowed, gitignored, non-secret regular files."""
        source_root = Path(self.repository.source_repository).resolve()
        target_root = worktree.resolve()
        forbidden_names = {
            "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
            "credentials.json", "service-account.json",
        }
        forbidden_suffixes = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
        for relative in self.environment_file_allowlist:
            rel = Path(relative)
            if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                raise ValueError(f"unsafe environment allowlist path: {relative}")
            source = (source_root / rel).resolve()
            try:
                source.relative_to(source_root)
            except ValueError as exc:
                raise ValueError(f"environment path escapes repository: {relative}") from exc
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"environment file must be a regular file: {relative}")
            if source.name.lower() in forbidden_names or source.suffix.lower() in forbidden_suffixes:
                raise PermissionError(f"private credential files cannot be copied: {relative}")
            ignored = _git(source_root, "check-ignore", "-q", "--", rel.as_posix(), check=False)
            if ignored.returncode != 0:
                raise ValueError(f"environment allowlist entry is not gitignored: {relative}")
            target = target_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if any(parent.is_symlink() for parent in (target, *target.parents)
                   if parent != target_root and target_root in parent.parents):
                raise ValueError(f"environment destination traverses symlink: {relative}")
            shutil.copy2(source, target, follow_symlinks=False)

    def get(self, run_id: str, agent_id: str) -> WorktreeLease | None:
        row = _get_conn().execute(
            "SELECT payload FROM worktree_leases WHERE run_id=? AND agent_id=? "
            "AND status='active' ORDER BY acquired_at DESC LIMIT 1", (run_id, agent_id),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload"])
        for field_name in ("acquired_at", "expires_at", "released_at"):
            if data.get(field_name):
                data[field_name] = datetime.fromisoformat(data[field_name])
        return WorktreeLease(**data)

    def release(self, lease: WorktreeLease) -> bool:
        status = _git(lease.worktree_path, "status", "--porcelain").stdout.strip()
        if status:
            return False
        # Do not delete commits that are not reachable from the base or an
        # integration ref.  Retaining the worktree is the recoverable choice.
        ahead = _git(lease.worktree_path, "rev-list", "--count",
                     f"{self.repository.base_commit_sha}..HEAD").stdout.strip()
        if int(ahead or "0") > 0:
            return False
        _git(self.repository.source_repository, "worktree", "remove", lease.worktree_path)
        lease.released_at = datetime.utcnow()
        self._save(lease, "released")
        return True

    @staticmethod
    def _save(lease: WorktreeLease, status: str) -> None:
        _get_conn().execute(
            "INSERT OR REPLACE INTO worktree_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lease.lease_id, lease.run_id, lease.agent_id, lease.worktree_path,
             lease.branch, json.dumps({
                 **lease.__dict__,
                 "acquired_at": lease.acquired_at.isoformat(),
                 "expires_at": lease.expires_at.isoformat(),
                 "released_at": lease.released_at.isoformat() if lease.released_at else None,
             }), status, lease.acquired_at.isoformat(), lease.expires_at.isoformat(),
             lease.released_at.isoformat() if lease.released_at else None),
        )
        _get_conn().commit()


class ConflictDetector:
    @staticmethod
    def changed_files(repo: str, base_sha: str, commit_sha: str = "HEAD") -> set[str]:
        result = _git(repo, "diff", "--name-only", f"{base_sha}...{commit_sha}")
        return {line for line in result.stdout.splitlines() if line}

    def overlaps(self, repo_a: str, commit_a: str, repo_b: str, commit_b: str,
                 base_sha: str) -> set[str]:
        return self.changed_files(repo_a, base_sha, commit_a) & self.changed_files(repo_b, base_sha, commit_b)


@dataclass
class MergeQueueItem:
    queue_id: str
    run_id: str
    agent_id: str
    commit_sha: str
    branch: str
    status: str = "queued"
    conflicts: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)


class MergeQueue:
    def enqueue(self, item: MergeQueueItem) -> None:
        _ensure_schema()
        now = datetime.utcnow().isoformat()
        _get_conn().execute(
            "INSERT INTO merge_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item.queue_id, item.run_id, item.agent_id, item.commit_sha, item.status,
             json.dumps({**item.__dict__, "created_at": item.created_at.isoformat()}), now, now),
        )
        _get_conn().commit()

    def list(self, run_id: str, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM merge_queue WHERE run_id=?"
        params: list[Any] = [run_id]
        if status:
            sql += " AND status=?"; params.append(status)
        return [json.loads(row["payload"]) for row in _get_conn().execute(sql, params).fetchall()]


class GitIntegrationManager:
    def __init__(
        self, repository: RepositoryWorkspaceManager,
        *, permission_broker: PermissionBroker | None = None,
        conflict_detector: ConflictDetector | None = None,
        merge_queue: MergeQueue | None = None,
    ) -> None:
        self.repository = repository
        self.permission_broker = permission_broker
        self.conflicts = conflict_detector or ConflictDetector()
        self.queue = merge_queue or MergeQueue()
        self.integration_path = Path(repository.run_workspace) / "integration" / "repo"
        self.integration_branch = f"integration/{_slug(Path(repository.run_workspace).name)}"

    def ensure_integration_worktree(self) -> str:
        if not self.integration_path.exists():
            result = _git(self.repository.source_repository, "worktree", "add", "-b",
                          self.integration_branch, str(self.integration_path),
                          self.repository.base_commit_sha, check=False)
            if result.returncode != 0 and "already exists" in result.stderr:
                _git(self.repository.source_repository, "worktree", "add",
                     str(self.integration_path), self.integration_branch)
            elif result.returncode != 0:
                raise RuntimeError(result.stderr.strip())
        return str(self.integration_path)

    def commit(self, lease: WorktreeLease, message: str, *, run_id: str,
               agent_id: str) -> str:
        if self.permission_broker is None:
            raise PermissionError("Git commit requires PermissionBroker")
        self.permission_broker.authorize(
            run_id=run_id, agent_id=agent_id, kind=PermissionKind.GIT_COMMIT,
            operation="git_commit", parameters={"branch": lease.branch,
                                                   "worktree": lease.worktree_path},
        )
        current = _git(lease.worktree_path, "branch", "--show-current").stdout.strip()
        if current in {"main", "master"}:
            raise PermissionError("coding teammate cannot commit protected branch")
        _git(lease.worktree_path, "add", "-A")
        if not _git(lease.worktree_path, "status", "--porcelain").stdout.strip():
            return _git(lease.worktree_path, "rev-parse", "HEAD").stdout.strip()
        _git(lease.worktree_path, "commit", "-m", message)
        sha = _git(lease.worktree_path, "rev-parse", "HEAD").stdout.strip()
        self.queue.enqueue(MergeQueueItem("merge_" + uuid.uuid4().hex[:16], run_id,
                                          agent_id, sha, lease.branch))
        return sha

    def integrate(self, item: MergeQueueItem) -> MergeQueueItem:
        path = self.ensure_integration_worktree()
        result = _git(path, "merge", "--no-ff", "--no-edit", item.commit_sha, check=False)
        if result.returncode != 0:
            conflicts = _git(path, "diff", "--name-only", "--diff-filter=U", check=False)
            item.status = "conflict"
            item.conflicts = [line for line in conflicts.stdout.splitlines() if line]
            _git(path, "merge", "--abort", check=False)
        else:
            item.status = "integrated"
        return item

    def verify_integration(self, argv: list[str], timeout: float = 300) -> Any:
        path = self.ensure_integration_worktree()
        return ShellCommandRunner().run(argv, cwd=path, timeout=timeout)

    def push(self, branch: str, *, run_id: str, agent_id: str,
             remote: str = "origin") -> None:
        if branch in {"main", "master"}:
            raise PermissionError("direct push to protected branch is forbidden")
        if self.permission_broker is None:
            raise PermissionError("Git push requires PermissionBroker")
        self.permission_broker.authorize(
            run_id=run_id, agent_id=agent_id, kind=PermissionKind.GIT_PUSH,
            operation="git_push", parameters={"remote": remote, "branch": branch},
        )
        _git(self.integration_path, "push", remote, branch)
