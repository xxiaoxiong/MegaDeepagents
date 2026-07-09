"""统一导出后端构建函数，供 agent_factory 使用。"""

import os
from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend, StateBackend
from deepagents.backends.filesystem import _raise_if_symlink_loop

from app.core.config import settings


def _normalize_windows_path(path: Path) -> Path:
    """Normalize Windows extended-length path prefix (\\?\) for consistent comparison."""
    s = str(path)
    if os.name == "nt":
        for prefix in ("\\\\?\\", "\\\\.\\"):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
    return Path(s)


class _SafeFilesystemBackend(FilesystemBackend):
    """修复 Windows 下 Path.resolve() 可能产生的 \\?\ 前缀不一致问题。"""

    def _resolve_path(self, key: str):
        if self.virtual_mode:
            vpath = key if key.startswith("/") else "/" + key
            if ".." in vpath or vpath.startswith("~"):
                msg = "Path traversal not allowed"
                raise ValueError(msg)
            full = (self.cwd / vpath.lstrip("/")).resolve()
            try:
                if os.name == "nt":
                    _normalize_windows_path(full).relative_to(_normalize_windows_path(self.cwd))
                else:
                    full.relative_to(self.cwd)
            except ValueError:
                msg = f"Path:{full} outside root directory: {self.cwd}"
                raise ValueError(msg) from None
            _raise_if_symlink_loop(full)
            return full

        path = Path(key)
        if path.is_absolute():
            _raise_if_symlink_loop(path)
            return path
        resolved = (self.cwd / path).resolve()
        _raise_if_symlink_loop(resolved)
        return resolved

    def _to_virtual_path(self, path: Path) -> str:
        cwd = _normalize_windows_path(self.cwd) if os.name == "nt" else self.cwd
        resolved = _normalize_windows_path(path.resolve()) if os.name == "nt" else path.resolve()
        return "/" + resolved.relative_to(cwd).as_posix()


def build_backend():
    ws = Path(settings.workspace_dir).resolve()
    skills = Path(settings.skills_dir).resolve()
    memory = Path(settings.memory_file).resolve().parent
    tool_results = ws / ".tool_results"

    routes = {
        "/workspace": _SafeFilesystemBackend(root_dir=ws, virtual_mode=True),
        "/skills": _SafeFilesystemBackend(root_dir=skills, virtual_mode=True),
        "/memory": _SafeFilesystemBackend(root_dir=memory, virtual_mode=True),
        "/large_tool_results": _SafeFilesystemBackend(root_dir=tool_results, virtual_mode=True),
    }

    # 沙箱集成：使用 DeepAgents 官方 LocalShellBackend
    if settings.sandbox_provider == "local":
        routes["/sandbox"] = LocalShellBackend(
            root_dir=Path(settings.sandbox_root_dir),
            virtual_mode=True,
            timeout=settings.sandbox_timeout,
        )

    return CompositeBackend(
        default=StateBackend(),
        routes=routes,
        artifacts_root="/",
    )
