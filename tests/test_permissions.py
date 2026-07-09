"""权限规则测试。"""

from deepagents.middleware.filesystem import _check_fs_permission, FilesystemPermission


def _make_rule(operations, paths, mode="allow"):
    return FilesystemPermission(operations=operations, paths=paths, mode=mode)


def test_env_denied():
    rules = [
        _make_rule(["read", "write"], ["/.env"], "deny"),
        _make_rule(["read", "write"], ["/**"], "allow"),
    ]
    assert _check_fs_permission(rules, "read", "/.env") == "deny"


def test_skills_write_denied():
    rules = [
        _make_rule(["write"], ["/skills/**"], "deny"),
        _make_rule(["read", "write"], ["/workspace/**"], "allow"),
        _make_rule(["read", "write"], ["/**"], "allow"),
    ]
    assert _check_fs_permission(rules, "write", "/skills/report-writer/SKILL.md") == "deny"


def test_workspace_write_allowed():
    rules = [
        _make_rule(["write"], ["/skills/**"], "deny"),
        _make_rule(["read", "write"], ["/workspace/**"], "allow"),
        _make_rule(["read", "write"], ["/**"], "allow"),
    ]
    assert _check_fs_permission(rules, "write", "/workspace/output.md") == "allow"


def test_memory_read_allowed():
    rules = [
        _make_rule(["read"], ["/memory/**"], "allow"),
        _make_rule(["read", "write"], ["/**"], "allow"),
    ]
    assert _check_fs_permission(rules, "read", "/memory/AGENTS.md") == "allow"
