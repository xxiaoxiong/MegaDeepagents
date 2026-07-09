"""权限规则：first-match-wins 的 filesystem 权限列表。"""

from deepagents.middleware.filesystem import FilesystemPermission


def build_permissions():
    return [
        # 敏感文件显式拒绝（必须在 allow 之前）
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/.env"],
            mode="deny",
        ),
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/secrets/**"],
            mode="deny",
        ),
        # Skills 写入拒绝（在通配 allow 之前拦截）
        FilesystemPermission(
            operations=["write"],
            paths=["/skills/**"],
            mode="deny",
        ),
        # 允许 workspace/memory 下的文件读写（backend virtual_mode 返回 /filename 格式）
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/*"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="allow",
        ),
        # Skills 只读（写入已被上面的规则拦截，这里补充读取放行）
        FilesystemPermission(
            operations=["read"],
            paths=["/skills/**"],
            mode="allow",
        ),
        # 兜底拒绝
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="deny",
        ),
    ]
