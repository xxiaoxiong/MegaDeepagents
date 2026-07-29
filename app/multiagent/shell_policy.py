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

    # Arguments that turn a "read-only" tool into a command-execution vector.
    # ``find -exec`` / ``-execdir`` run an arbitrary binary on every match;
    # ``sed -i`` mutates files in place and the ``e`` command executes shell.
    # These must be escalated past the READ_ONLY fast-path so the permission
    # broker gets to authorize them.
    _EXEC_FLAGS = {"-exec", "-execdir", "-ok", "-okdir"}

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
        # Windows resolves Python and other build tools through ``*.exe``.
        # Classify the basename consistently whether callers pass ``python``
        # or an absolute interpreter path such as ``C:\...\python.exe``.
        for suffix in (".exe", ".cmd", ".bat"):
            executable = executable.removesuffix(suffix)
        if executable in {"powershell", "pwsh"}:
            return self._classify_powershell(argv)
        if executable == "cmd":
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
        # ``find``/``sed`` are nominally read-only, but their exec/inplace
        # flags turn them into arbitrary command execution or file mutation.
        # Escalate before the READ_ONLY fast-path so the permission broker
        # authorizes the destructive form.  ``find . -exec cat /etc/passwd \;``
        # must not bypass the broker.
        if executable == "find" and self._has_exec_payload(argv):
            return CommandCategory.FILESYSTEM_DESTRUCTIVE
        if executable == "sed" and self._sed_is_destructive(argv):
            return CommandCategory.FILESYSTEM_DESTRUCTIVE
        if executable in self.BUILD_TEST:
            # Package install through npm/pnpm/yarn is still package management.
            if executable in {"npm", "pnpm", "yarn"} and len(argv) > 1 and argv[1] in {"i", "install", "add"}:
                return CommandCategory.PACKAGE_MANAGEMENT
            # ``python -c "import os; os.system('rm -rf /')"``, ``node -e
            # "require('fs').writeFileSync(...)"``, ``npm run <script>`` and
            # similar inline-script flags turn a build/test launcher into an
            # arbitrary code execution vector.  They are not "build/test"
            # activity even though the host binary is in BUILD_TEST — escalate
            # to SHELL so the PermissionBroker must authorize them, instead of
            # letting them through the unattended BUILD_TEST fast-path.
            if self._has_inline_script(executable, argv):
                return CommandCategory.UNKNOWN
            return CommandCategory.BUILD_TEST
        if executable in self.READ_ONLY:
            return CommandCategory.READ_ONLY
        return CommandCategory.UNKNOWN

    @classmethod
    def _has_exec_payload(cls, argv: list[str]) -> bool:
        """``find -exec`` / ``-execdir`` run an arbitrary binary per match."""
        for arg in argv[1:]:
            if arg in cls._EXEC_FLAGS:
                return True
        return False

    # Inline-script flags per build/test launcher.  These take the *next*
    # argument (or the rest of a clustered option) as a program to execute
    # directly, so ``python -c "import os; os.system('rm -rf /')"`` /
    # ``node -e "..."`` are equivalent to running an arbitrary script under
    # the host interpreter.  Script runners like ``npm run <name>`` are
    # deliberately NOT included here: they invoke a predefined script in
    # ``package.json`` rather than inline code, and the integration
    # verification path already filters them through ``_safe_integration_argv``.
    _INLINE_SCRIPT_FLAGS: dict[str, frozenset[str]] = {
        "python": frozenset({"-c"}),
        "python3": frozenset({"-c"}),
        "node": frozenset({"-e", "--eval", "-p", "--print"}),
        "ruby": frozenset({"-e"}),
        "perl": frozenset({"-e", "-E"}),
        "php": frozenset({"-r"}),
    }

    @classmethod
    def _has_inline_script(cls, executable: str, argv: list[str]) -> bool:
        """True when a BUILD_TEST launcher is asked to run inline code.

        ``python -c "..."`` / ``node -e "..."`` escape the "this is just a
        build/test invocation" assumption: they can execute arbitrary code,
        including shelling out via ``os.system``.  We return True so the
        caller escalates the category past BUILD_TEST and forces
        PermissionBroker authorization.
        """
        flags = cls._INLINE_SCRIPT_FLAGS.get(executable)
        if not flags:
            return False
        for arg in argv[1:]:
            # Stop scanning at the first non-option argument: a leading
            # ``-`` afterwards is most likely a flag to the script itself
            # (e.g. ``python script.py -c``), not an interpreter flag.
            if not arg.startswith("-"):
                break
            # ``-c`` may be folded into a cluster like ``-cO`` for python.
            token = arg.lstrip("-")
            if any(f.lstrip("-") == token or token.startswith(f.lstrip("-"))
                   for f in flags):
                return True
            # ``--eval``-style long options
            if arg in flags:
                return True
        return False

    @staticmethod
    def _sed_is_destructive(argv: list[str]) -> bool:
        """``sed -i`` mutates in place; the ``e`` command executes shell."""
        inplace = False
        for arg in argv[1:]:
            if arg.startswith("-"):
                # GNU sed folds options like ``-iE``; ``-i`` anywhere in the
                # cluster enables in-place editing.
                if "i" in arg:
                    inplace = True
            elif "e" in arg and arg.startswith("e"):
                # A script argument beginning with ``e`` (sed `e` command)
                # executes the rest of the line as shell.  Conservative match.
                return True
            else:
                # The first non-option argument is the script; ``e`` command
                # inside it executes shell.
                if "e" in arg.split("\n")[0] and (";" in arg or arg.startswith("e")):
                    return True
                break
        return inplace

    @staticmethod
    def workspace_escape(argv: list[str], root: Path) -> str | None:
        """Return the first READ_ONLY argument that leaves the task workspace.

        READ_ONLY commands (``head``/``tail``/``cat``/``grep``/``find``/...)
        bypass the permission broker, so a path argument that resolves outside
        ``root`` would let an agent read ``/runtime/.env`` or
        ``/etc/passwd`` without authorization.  Flags are skipped; only
        path-like arguments are inspected.  An argument is path-like when it
        is absolute, contains a path separator, or contains a ``..`` segment.
        """
        for arg in argv[1:]:
            if not arg or arg.startswith("-"):
                continue
            is_absolute = (
                arg.startswith("/")
                or (len(arg) >= 3 and arg[1] == ":" and arg[2] in ("\\", "/"))
            )
            has_separator = "/" in arg or "\\" in arg
            has_dotdot = ".." in Path(arg).parts
            if not (is_absolute or has_separator or has_dotdot):
                # A bare token (e.g. a grep pattern or filename) cannot escape
                # by itself; leave it to the OS to resolve inside cwd.
                continue
            try:
                candidate = (root / arg).resolve() if not is_absolute else Path(arg).resolve()
            except (OSError, ValueError):
                return arg
            try:
                candidate.relative_to(root)
            except ValueError:
                return arg
        return None

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
        # READ_ONLY commands bypass the PermissionBroker, so they must not be
        # able to read files outside the task workspace.  ``head /runtime/.env``
        # or ``cat ../../etc/passwd`` would otherwise leak secrets straight
        # into the tool result stream.  Reject any path argument that is
        # absolute or escapes ``cwd`` before execution.
        if category == CommandCategory.READ_ONLY:
            escaped = self.policy.workspace_escape(argv, root)
            if escaped is not None:
                raise PermissionError(
                    f"read-only command references path outside workspace: {escaped}"
                )
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
        if os.name == "nt" and Path(argv[0]).name.lower() == "echo":
            # ``echo`` is a cmd.exe built-in on Windows.  Handling it here
            # preserves argv semantics without introducing a shell parser.
            return ShellResult(
                argv=argv,
                returncode=0,
                stdout=" ".join(argv[1:]) + "\n",
                duration_seconds=time.monotonic() - start,
                environment={"platform": os.name, "cwd": str(root)},
            )
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
                # CTRL_BREAK_EVENT is delivered to the whole console group and
                # can interrupt the host test/API process.  Terminate the child
                # directly, then escalate to kill below if it does not exit.
                process.terminate()
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
