"""Structured shell policy and cancellable subprocess execution."""
from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from app.multiagent.permission import PermissionBroker, PermissionKind


class CommandCategory(str, Enum):
    READ_ONLY = "read_only"
    BUILD_TEST = "build_test"
    PACKAGE_MANAGEMENT = "package_management"
    NETWORK = "network"
    GIT_WRITE = "git_write"
    FILESYSTEM_DESTRUCTIVE = "filesystem_destructive"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    UNKNOWN = "unknown"


@dataclass
class ShellResult:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    cancellation_phase: str | None = None
    duration_seconds: float = 0.0
    environment: dict[str, str] = field(default_factory=dict)


class ShellPolicyEngine:
    """Classify argv, never an interpolated shell string."""

    READ_ONLY = {"ls", "dir", "pwd", "find", "rg", "grep", "head", "tail", "wc", "echo",
                 "sed", "type", "where", "which", "git"}
    BUILD_TEST = {"pytest", "python", "python3", "node", "npm", "pnpm", "yarn",
                  "make", "cmake", "cargo", "go", "mvn", "gradle", "ruff", "mypy",
                  "eslint", "tsc"}
    PACKAGE = {"pip", "pip3", "uv", "poetry", "conda", "apt", "apt-get", "brew",
               "choco", "winget"}
    NETWORK = {"curl", "wget", "ssh", "scp", "nc", "ncat", "telnet"}
    DESTRUCTIVE = {"rm", "rmdir", "del", "erase", "format", "mkfs", "dd", "shred"}
    PRIVILEGE = {"sudo", "su", "doas", "runas"}
    GIT_WRITE = {"add", "commit", "push", "merge", "rebase", "reset", "checkout",
                 "switch", "branch", "cherry-pick", "tag", "clean"}
    GIT_READ = {"status", "diff", "log", "show", "rev-parse", "ls-files", "remote"}
    POWERSHELL_READ = {"get-childitem", "get-content", "get-item", "get-location",
                       "test-path", "select-string", "measure-object"}
    POWERSHELL_DESTRUCTIVE = {"remove-item", "clear-content", "format-volume"}
    POWERSHELL_NETWORK = {"invoke-webrequest", "invoke-restmethod", "new-pssession"}

    def normalize(self, command: Sequence[str] | str) -> list[str]:
        if isinstance(command, str):
            # Parsing is only a compatibility adapter.  Execution always uses
            # the resulting argv with shell=False; shell operators stay plain
            # arguments and cannot become an injection boundary.
            return shlex.split(command, posix=os.name != "nt")
        return [str(part) for part in command]

    def classify(self, command: Sequence[str] | str) -> CommandCategory:
        argv = self.normalize(command)
        if not argv:
            return CommandCategory.UNKNOWN
        executable = Path(argv[0]).name.lower()
        if executable in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
            return self._classify_powershell(argv)
        if executable in {"cmd", "cmd.exe"}:
            return self._classify_cmd(argv)
        if executable in self.PRIVILEGE:
            return CommandCategory.PRIVILEGE_ESCALATION
        if executable in self.DESTRUCTIVE:
            return CommandCategory.FILESYSTEM_DESTRUCTIVE
        if executable in self.NETWORK:
            return CommandCategory.NETWORK
        if executable in self.PACKAGE:
            return CommandCategory.PACKAGE_MANAGEMENT
        if executable == "git":
            subcommand = argv[1].lower() if len(argv) > 1 else ""
            if subcommand in self.GIT_WRITE:
                return CommandCategory.GIT_WRITE
            if subcommand in self.GIT_READ:
                return CommandCategory.READ_ONLY
            return CommandCategory.UNKNOWN
        if executable in self.BUILD_TEST:
            # Package install through npm/pnpm/yarn is still package management.
            if executable in {"npm", "pnpm", "yarn"} and len(argv) > 1 and argv[1] in {"i", "install", "add"}:
                return CommandCategory.PACKAGE_MANAGEMENT
            return CommandCategory.BUILD_TEST
        if executable in self.READ_ONLY:
            return CommandCategory.READ_ONLY
        return CommandCategory.UNKNOWN

    def _classify_powershell(self, argv: list[str]) -> CommandCategory:
        lowered = [part.lower() for part in argv[1:]]
        try:
            marker = next(i for i, part in enumerate(lowered) if part in {"-command", "-c"})
        except StopIteration:
            return CommandCategory.UNKNOWN
        script = " ".join(argv[marker + 2:]).strip()
        # PowerShell expands these metacharacters itself even though the host
        # process uses shell=False.  Compound scripts always require review.
        if not script or re.search(r"[;&|`]|\$\(|\n|\r", script):
            return CommandCategory.UNKNOWN
        verb = script.split()[0].lower()
        if verb in self.POWERSHELL_DESTRUCTIVE:
            return CommandCategory.FILESYSTEM_DESTRUCTIVE
        if verb in self.POWERSHELL_NETWORK:
            return CommandCategory.NETWORK
        if verb in self.POWERSHELL_READ:
            return CommandCategory.READ_ONLY
        return CommandCategory.UNKNOWN

    def _classify_cmd(self, argv: list[str]) -> CommandCategory:
        if len(argv) < 3 or argv[1].lower() not in {"/c", "/k"}:
            return CommandCategory.UNKNOWN
        command = " ".join(argv[2:]).strip()
        if not command or re.search(r"[&|<>^\n\r]", command):
            return CommandCategory.UNKNOWN
        executable = command.split()[0].lower()
        if executable in {"dir", "type", "where", "find", "findstr"}:
            return CommandCategory.READ_ONLY
        if executable in {"del", "erase", "rmdir", "format"}:
            return CommandCategory.FILESYSTEM_DESTRUCTIVE
        return CommandCategory.UNKNOWN

    @staticmethod
    def permission_kind(category: CommandCategory) -> PermissionKind:
        return {
            CommandCategory.PACKAGE_MANAGEMENT: PermissionKind.PACKAGE_INSTALL,
            CommandCategory.NETWORK: PermissionKind.NETWORK,
            CommandCategory.GIT_WRITE: PermissionKind.GIT_COMMIT,
            CommandCategory.FILESYSTEM_DESTRUCTIVE: PermissionKind.DESTRUCTIVE,
            CommandCategory.PRIVILEGE_ESCALATION: PermissionKind.DESTRUCTIVE,
        }.get(category, PermissionKind.SHELL)


class ShellCommandRunner:
    def __init__(
        self, *, policy: ShellPolicyEngine | None = None,
        permission_broker: PermissionBroker | None = None,
        output_limit: int = 32_000,
    ) -> None:
        self.policy = policy or ShellPolicyEngine()
        self.permission_broker = permission_broker
        self.output_limit = output_limit

    def run(
        self, command: Sequence[str] | str, *, cwd: str,
        run_id: str = "", agent_id: str = "", timeout: float = 30,
        cancel_token: Any | None = None,
    ) -> ShellResult:
        argv = self.policy.normalize(command)
        category = self.policy.classify(argv)
        if not argv:
            raise ValueError("empty command")
        root = Path(cwd).resolve()
        if not root.is_dir():
            raise ValueError(f"cwd does not exist: {cwd}")
        if category in (CommandCategory.FILESYSTEM_DESTRUCTIVE,
                        CommandCategory.PRIVILEGE_ESCALATION):
            # These are never silently converted into a normal shell request.
            kind = self.policy.permission_kind(category)
        else:
            kind = self.policy.permission_kind(category)
        if self.permission_broker is not None and category not in (
            CommandCategory.READ_ONLY, CommandCategory.BUILD_TEST,
        ):
            self.permission_broker.authorize(
                run_id=run_id, agent_id=agent_id, kind=kind,
                operation="shell_execute",
                parameters={"argv": argv, "cwd": str(root), "category": category.value},
                reason=f"execute {category.value} command",
            )
        elif category in (CommandCategory.UNKNOWN, CommandCategory.FILESYSTEM_DESTRUCTIVE,
                           CommandCategory.PRIVILEGE_ESCALATION,
                           CommandCategory.NETWORK, CommandCategory.PACKAGE_MANAGEMENT,
                           CommandCategory.GIT_WRITE):
            raise PermissionError(f"command requires PermissionBroker: {category.value}")
        if cancel_token is not None and cancel_token.is_set():
            return ShellResult(argv=argv, returncode=-1, cancelled=True,
                               cancellation_phase="cancelled_before_tool")

        start = time.monotonic()
        creationflags = 0
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(
            argv, shell=False, cwd=str(root), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, creationflags=creationflags,
            **popen_kwargs,
        )
        cancelled = False
        timed_out = False
        deadline = start + timeout
        while process.poll() is None:
            if cancel_token is not None and cancel_token.is_set():
                cancelled = True
                self._terminate_tree(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._terminate_tree(process)
                break
            time.sleep(0.05)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        return ShellResult(
            argv=argv, returncode=process.returncode if process.returncode is not None else -1,
            stdout=(stdout or "")[:self.output_limit], stderr=(stderr or "")[:self.output_limit],
            timed_out=timed_out, cancelled=cancelled,
            cancellation_phase="cancelled_during_tool" if cancelled else None,
            duration_seconds=time.monotonic() - start,
            environment={"platform": os.name, "cwd": str(root)},
        )

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[Any]) -> None:
        try:
            if os.name == "nt":
                process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1.5)
        except Exception:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
