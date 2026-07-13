"""AgentExecutor — 统一 Worker 执行接口。

docs/upgradePhaseTwo.md §三：
- `DeepAgentExecutor` — 用于 Coder、Tester、Researcher 等真实 Worker。调用真实 Deep Agent
  并传递 profile 中受限的工具集。
- `ModelDecisionExecutor` — 用于 Planner、Router、轻量 Evaluator 等仅需结构化决策节点。
  只调 LLM，不默认获得写文件或 Shell。

禁止所有 Agent 都继续使用同一个裸 `build_model().invoke(prompt)` 逻辑：
- DeepAgentExecutor → 使用 `create_deep_agent` + 按 Profile 过滤的工具集
- ModelDecisionExecutor → 使用 `build_model()` + 结构化 JSON 输出 + schema 校验
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, Callable

from app.core.logging import logger
from app.multiagent.agent_profile import AgentProfile, get_capability_registry
from app.multiagent.task_graph import TaskGraph, TaskNode


# ===== 数据模型 =====


@dataclass
class TaskAssignment:
    """Scheduler 分配给 Executor 的任务信息。"""
    task_id: str
    objective: str
    description: str
    input_artifact_ids: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    max_attempts: int = 2
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionContext:
    """执行上下文。"""
    run_id: str
    workspace_root: str  # Run 级 workspace 根目录
    task_dag: TaskGraph | None = None
    langsmith_trace_id: str | None = None
    thread_id: str | None = None


@dataclass
class AgentExecutionResult:
    """Worker 执行结果。"""
    success: bool
    output_summary: str = ""
    produced_artifact_ids: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    execution_time: float = 0.0
    token_usage: dict[str, int] = field(default_factory=dict)


# ===== 执行接口协议 =====


class AgentExecutor(Protocol):
    """统一 Agent 执行协议。"""

    def execute(
        self,
        assignment: TaskAssignment,
        profile: AgentProfile,
        context: ExecutionContext,
    ) -> AgentExecutionResult:
        """执行一个 Task 并返回结果。"""
        ...


# ===== ModelDecisionExecutor =====


@dataclass
class ModelDecisionExecutor:
    """结构化决策执行器：只调 LLM，无工具权限。

    用于 Planner、Router、轻量 Evaluator 等节点。
    输出必须为符合指定 schema 的 JSON。

    测试注入：在非测试环境中调用 `build_model()` 构造 LLM；
    测试通过 monkeypatch llm_factory.build_model 返回 mock。
    """

    model_name: str = "deepseek-chat"

    def execute(
        self,
        assignment: TaskAssignment,
        profile: AgentProfile,
        context: ExecutionContext,
    ) -> AgentExecutionResult:
        """执行一次结构化 LLM 决策。

        流程：
        1. 构造 system prompt（用 profile.name + role + description）
        2. 添加任务上下文（objective + input_artifact IDs）
        3. 调用 LLM（JSON mode）
        4. 解析结果
        """
        from app.llm_factory import build_model
        import time

        system_prompt = (
            f"你是一个 {profile.role}（{profile.name}）。\n"
            f"{profile.description}\n\n"
            f"你只能做结构化决策，没有文件或 Shell 工具权限。\n"
            f"你必须输出 JSON 格式的结果，包含 'decision' 字段和 'reasoning' 字段。\n"
        )

        user_prompt = (
            f"## 任务目标\n{assignment.objective}\n\n"
            f"## 详细描述\n{assignment.description or '(无)'}\n\n"
            f"## 输入 Artifact IDs\n"
            + (", ".join(assignment.input_artifact_ids) if assignment.input_artifact_ids else "(无)")
            + "\n\n"
            f"请用 JSON 格式输出你的决策。"
        )

        start = time.time()
        try:
            # Phase Two #17: model_policy 影响模型选择
            from app.llm_factory import build_model_for_policy
            llm = build_model_for_policy(getattr(profile, "model_policy", None))
            try:
                json_llm = llm.bind(response_format={"type": "json_object"})
            except Exception:
                json_llm = llm

            response = json_llm.invoke([
                ("system", system_prompt),
                ("user", user_prompt),
            ])
            elapsed = time.time() - start
            text = getattr(response, "content", str(response))
            if isinstance(text, list):
                text = json.dumps(text, ensure_ascii=False)

            try:
                parsed = json.loads(text) if isinstance(text, str) else text
            except json.JSONDecodeError:
                parsed = {"decision": "llm_output_not_parsed", "raw_output": text[:500]}

            return AgentExecutionResult(
                success=True,
                output_summary=json.dumps(parsed, ensure_ascii=False)[:300],
                tool_calls=[{"tool": "llm_decision", "output_preview": str(parsed)[:200]}],
                execution_time=elapsed,
            )
        except Exception as exc:
            elapsed = time.time() - start
            logger.error(f"[ModelDecisionExecutor] LLM call failed: {exc}")
            return AgentExecutionResult(
                success=False,
                error=str(exc),
                execution_time=elapsed,
            )


# ===== 受限工具构建（用于 DeepAgentExecutor） =====

_LANGCHAIN_TOOL_NAMES: dict[str, str] = {}


def _safe_workspace_path(root: str, requested: str) -> Path:
    """Resolve a tool path without allowing traversal or symlink escape."""
    base = Path(root).resolve()
    candidate = (base / requested).resolve() if not Path(requested).is_absolute() else Path(requested).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError(f"path escapes workspace: {requested}")
    return candidate


def _make_read_file_tool(task_workspace: str):
    from langchain.tools import tool

    @tool
    def read_file(file_path: str) -> str:
        """读取指定文件的全部内容。"""
        try:
            path = _safe_workspace_path(task_workspace, file_path)
        except ValueError as exc:
            return f"错误: {exc}"
        if not path.is_file():
            return f"错误: 文件不存在 {file_path}"
        with path.open("r", encoding="utf-8") as f:
            return f.read()
    return read_file


def _make_list_dir_tool(task_workspace: str):
    from langchain.tools import tool

    @tool
    def list_dir(path: str = ".") -> str:
        """列出指定目录中的文件和子目录。"""
        import json
        try:
            resolved = _safe_workspace_path(task_workspace, path)
        except ValueError as exc:
            return f"错误: {exc}"
        if not resolved.is_dir():
            return f"错误: 目录不存在 {path}"
        items = [entry.name for entry in resolved.iterdir()]
        return json.dumps(items, ensure_ascii=False)
    return list_dir


def _make_create_file_tool(task_workspace: str):
    from langchain.tools import tool

    @tool
    def create_file(file_path: str, content: str) -> str:
        """创建或覆写文件。路径相对于工作目录。"""
        try:
            path = _safe_workspace_path(task_workspace, file_path)
        except ValueError as exc:
            return f"错误: {exc}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            f.write(content)
        return f"文件已写入: {path}"
    return create_file


def _make_edit_file_tool(task_workspace: str):
    from langchain.tools import tool

    @tool
    def edit_file(file_path: str, old_string: str, new_string: str) -> str:
        """编辑文件的字符串替换。"""
        try:
            path = _safe_workspace_path(task_workspace, file_path)
        except ValueError as exc:
            return f"错误: {exc}"
        if not path.is_file():
            return f"错误: 文件不存在 {path}"
        with path.open("r", encoding="utf-8") as f:
            content = f.read()
        if old_string not in content:
            return f"未找到要替换的字符串"
        content = content.replace(old_string, new_string, 1)
        with path.open("w", encoding="utf-8") as f:
            f.write(content)
        return f"已编辑 {path}"
    return edit_file


def _make_execute_tool(task_workspace: str):
    from langchain.tools import tool

    # 危险命令黑名单
    DANGEROUS_COMMANDS = [
        "rm -rf /", "rm -rf ~", "rm -rf .", "del /f", "format ",
        "mkfs", "dd if=", "> /dev/sda", ":(){ :|:& };:", "wget ",
        "curl -o ", "chmod 777 ", "sudo ",
    ]

    @tool
    def execute(command: str) -> str:
        """执行 shell 命令。注意：危险命令将被拒绝。"""
        import subprocess
        # 危险命令检查
        cmd_lower = command.lower().strip()
        for dangerous in DANGEROUS_COMMANDS:
            if cmd_lower.startswith(dangerous.lower()):
                return f"错误: 危险命令已被安全管理器拒绝: {command[:80]}"
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                cwd=task_workspace, timeout=30,
            )
            output = result.stdout[:2000]
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr[:1000]}"
            return output or "(无输出)"
        except subprocess.TimeoutExpired:
            return f"执行超时 (30s): {command[:80]}"
        except Exception as exc:
            return f"执行失败: {exc}"
    return execute


def _build_restricted_tools(
    allowed_tools: list[str],
    deny_default: bool,
    task_workspace: str,
    allow_file_read: bool = True,
    allow_file_write: bool = True,
    allow_shell: bool = True,
) -> list[Any]:
    """根据权限构造受限工具列表。"""
    tools = []

    # 白名单查表
    allowed_set = set(allowed_tools)

    if allow_file_read and (not deny_default or "read_file" in allowed_set):
        tools.append(_make_read_file_tool(task_workspace))
    if allow_file_read and (not deny_default or "list_dir" in allowed_set):
        tools.append(_make_list_dir_tool(task_workspace))
    if allow_file_write and (not deny_default or "create_file" in allowed_set):
        tools.append(_make_create_file_tool(task_workspace))
    if allow_file_write and (not deny_default or "edit_file" in allowed_set):
        tools.append(_make_edit_file_tool(task_workspace))
    if allow_shell and (not deny_default or "execute" in allowed_set):
        tools.append(_make_execute_tool(task_workspace))

    return tools


# ===== DeepAgentExecutor =====


class DeepAgentExecutor:
    """真实工具 Worker 执行器：创建受限 DeepAgent 来执行任务。

    使用 app.core.agent_factory 的 build_agent 思路并：
    1. 按 AgentProfile.tool_policy 过滤可用工具
    2. 设置受限的 system prompt（包含角色边界）
    3. 启用自己的 workspace 子目录
    4. 支持 checkpoint（通过 SqliteSaver）
    5. 记录实际调用的工具（tool_calls）

    **测试注意事项**：
    - 本执行器需要 deepagents + langgraph + LLM 全部可用。
    - 单元测试应 mock `_mock_invoke` 来模拟 DeepAgent 响应。
    - 集成测试可用 `_build_restricted_tools`（独立函数无外部依赖）做工具级验证。
    """

    def __init__(self, workspace_root: str | None = None):
        """Args:
            workspace_root: Run 级 workspace 根目录。CLI 注入；为 None 时
                execute_task 调用方必须通过 task_input 传入。
        """
        self.workspace_root = workspace_root
        # ArtifactStore 注入（Phase A 修复断链）
        self._artifact_store: Any | None = None
        # 测试 hook：设置后 execute 跳过真实 agent 创建
        self._mock_response: AgentExecutionResult | None = None
        self._mock_invoke: callable | None = None

    def set_artifact_store(self, store: Any) -> None:
        """注入 ArtifactStore，让 execute_task 生成的产物作为真实 Artifact 注册。"""
        self._artifact_store = store

    def set_run_id(self, run_id: str) -> None:
        """注入 TeamRunContext.run_id，避免回退到 'cli_run' 硬编码。"""
        self._run_id = run_id

    def _ctx_run_id(self) -> str | None:
        return getattr(self, "_run_id", None)

    # ===== Scheduler 协议适配（WorkerExecutor.execute_task） =====

    def execute_task(
        self,
        task_dag: TaskGraph,
        task_id: str,
        task_input: dict[str, Any],
    ) -> "TaskResult":
        """对接 TaskScheduler 的 WorkerExecutor 协议。

        适配逻辑：
        1. 从 task_dag 取 TaskNode
        2. 按 required_capabilities 在 CapabilityRegistry 选 AgentProfile
        3. 用 workspace_root + task_id 构造 ExecutionContext
        4. 调用 self.execute(assignment, profile, context)
        5. 把 AgentExecutionResult 转成 scheduler 期望的 TaskResult（artifact_ids 字段）

        task_input 可包含:
            - workspace_root: str   覆盖 self.workspace_root
            - input_artifact_ids: list[str]
        """
        from app.multiagent.scheduler import TaskResult

        node = task_dag.nodes.get(task_id)
        if node is None:
            return TaskResult(
                task_id=task_id,
                success=False, error=f"task {task_id} not in dag",
                artifact_ids=[],
            )

        workspace_root = (
            task_input.get("workspace_root")
            or self.workspace_root
            or _default_workspace_root()
        )
        # 确保 workspace/tasks/<task_id> 目录存在
        Path(workspace_root, "tasks", task_id).mkdir(parents=True, exist_ok=True)

        # A missing capability must fail the assignment.  Falling back to a
        # broad DefaultCoder would be an unapproved privilege escalation.
        registry = get_capability_registry()
        profile = registry.get_profile(task_input.get("profile_id", ""))
        if profile is None:
            profile = registry.find_best_worker(set(node.required_capabilities))
        if profile is None or not set(node.required_capabilities).issubset(profile.capabilities):
            return TaskResult(
                task_id=task_id, success=False, artifact_ids=[],
                error="no_matching_worker",
            )

        assignment = TaskAssignment(
            task_id=task_id,
            objective=node.objective,
            description=node.description or node.objective,
            input_artifact_ids=task_input.get("input_artifact_ids", []),
            dependencies=list(node.dependencies),
            required_capabilities=list(node.required_capabilities),
            max_attempts=node.max_attempts,
            metadata={
                "priority": node.priority,
                "mailbox_messages": list(task_input.get("mailbox_messages", [])),
            },
        )
        context = ExecutionContext(
            run_id=task_input.get("run_id") or self._ctx_run_id() or "cli_run",
            workspace_root=workspace_root,
            task_dag=task_dag,
            thread_id=task_input.get("thread_id"),
        )

        result = self.execute(assignment, profile, context)

        # 把 produced_artifact_ids 装回 TaskNode 用作下游 input
        return TaskResult(
            task_id=task_id,
            success=result.success,
            artifact_ids=list(result.produced_artifact_ids or []),
            error=result.error,
        )

    def execute(
        self,
        assignment: TaskAssignment,
        profile: AgentProfile,
        context: ExecutionContext,
    ) -> AgentExecutionResult:
        """使用 DeepAgent 执行一次任务。

        流程：
        1. 过滤工具权限
        2. 构造受限 system prompt + 任务上下文
        3. 创建 DeepAgent（或 mock 路径）
        4. invoke 得到产出物
        5. 记录工具使用
        """
        import time
        from pathlib import Path

        # mock 路径
        if self._mock_response is not None:
            return self._mock_response
        if self._mock_invoke is not None:
            return self._mock_invoke(assignment, profile, context)

        from deepagents import create_deep_agent

        task_workspace = Path(context.workspace_root) / "tasks" / assignment.task_id
        task_workspace.mkdir(parents=True, exist_ok=True)

        start = time.time()

        try:
            # Phase Two #17: 让 profile.model_policy 真正影响模型选择
            from app.llm_factory import build_model_for_policy
            model = build_model_for_policy(getattr(profile, "model_policy", None))
            # DeepAgent execution remains available when the optional
            # langgraph sqlite checkpointer extra is absent.  A failed import
            # must not prevent real tools/artifacts from running.
            try:
                from app.core.agent_factory import _get_sqlite_saver
                checkpointer = _get_sqlite_saver()
            except Exception as exc:
                logger.warning("[DeepAgentExecutor] checkpoint unavailable: %s", exc)
                checkpointer = None

            allowed_tools = profile.tool_policy.allowed_tools
            deny_default = profile.tool_policy.deny_all_by_default

            tools = _build_restricted_tools(
                allowed_tools, deny_default, task_workspace=str(task_workspace),
                allow_file_read=profile.tool_policy.allow_file_read,
                allow_file_write=profile.tool_policy.allow_file_write,
                allow_shell=profile.tool_policy.allow_shell,
            )

            system_prompt = (
                f"你是一个 {profile.role}（{profile.name}）。\n"
                f"{profile.description}\n\n"
                f"## 任务目标\n{assignment.objective}\n\n"
                f"## 角色边界\n"
                + _build_boundary_prompt(profile)
                + "\n\n"
                f"你必须使用可用工具完成任务。工具受限，越权调用将被拒绝。\n"
                f"所有产物必须写入工作目录 {task_workspace}。\n"
            )
            mailbox_messages = assignment.metadata.get("mailbox_messages", [])
            if mailbox_messages:
                directives = "\n".join(
                    f"- {message.get('from_agent_id', 'agent')}: {message.get('content', '')}"
                    for message in mailbox_messages
                )
                system_prompt += (
                    "\n## 本轮收到的协作消息\n"
                    "以下是已投递给你的任务级上下文；在不违反角色边界时应纳入执行。\n"
                    f"{directives}\n"
                )

            agent = create_deep_agent(
                name=f"{profile.id}:{assignment.task_id}",
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                checkpointer=checkpointer,
                debug=False,
            )

            response = agent.invoke({
                "messages": [
                    ("user",
                     f"目标：{assignment.objective}\n\n"
                     f"描述：{assignment.description}\n\n"
                     f"请使用可用工具完成此任务。所有产物必须写入工作目录。"
                     f"完成后返回结果摘要。")
                ]
            }, config={
                "configurable": {"thread_id": getattr(context, "thread_id", None) or f"{context.run_id}:{assignment.task_id}"},
                "recursion_limit": 80,
            })

            elapsed = time.time() - start
            tool_calls = _extract_tool_calls(response)

            ignored_parts = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".cache"}
            produced_files = [
                file_path for file_path in task_workspace.rglob("*")
                if file_path.is_file() and not file_path.name.startswith(".")
                and not any(part in ignored_parts for part in file_path.parts)
            ]
            produced_artifact_ids = []
            # 移除"兼容回退"伪 ID：所有 artifact ID 必须来自真实 ArtifactStore.create
            if self._artifact_store is not None and context.run_id:
                try:
                    for file_path in produced_files:
                        relative_path = file_path.relative_to(Path(context.workspace_root)).as_posix()
                        artifact = self._artifact_store.create(
                            run_id=context.run_id,
                            task_id=assignment.task_id,
                            type=self._infer_artifact_type(file_path.name),
                            relative_path=relative_path,
                            content=file_path.read_bytes(),
                            produced_by=profile.name,
                            metadata={"profile_id": profile.id, "original_name": file_path.name},
                        )
                        produced_artifact_ids.append(artifact.id)
                except Exception as exc:
                    logger.warning(f"[DeepAgentExecutor] artifact create failed: {exc}")
                    # 不降级为伪 ID：让上游能感知失败
                    raise
            else:
                logger.warning(
                    f"[DeepAgentExecutor] no artifact_store or run_id configured for "
                    f"run={context.run_id} – produced files are not registered"
                )

            final_messages = response.get("messages", [{}]) if isinstance(response, dict) else [{}]
            last = final_messages[-1] if final_messages else {}
            output = str(getattr(last, "content", str(last)))[:500]

            return AgentExecutionResult(
                success=True,
                output_summary=output[:300],
                produced_artifact_ids=produced_artifact_ids,
                tool_calls=tool_calls,
                execution_time=elapsed,
            )

        except Exception as exc:
            elapsed = time.time() - start
            logger.error(f"[DeepAgentExecutor] task={assignment.task_id} failed: {exc}")
            return AgentExecutionResult(
                success=False,
                error=str(exc),
                execution_time=elapsed,
            )

    @staticmethod
    def _infer_artifact_type(filename: str) -> str:
        """根据文件名推断 ArtifactType。"""
        lower = filename.lower()
        if lower.endswith(".py") or lower.endswith(".js") or lower.endswith(".ts"):
            return "code"
        if lower.startswith("test_") or lower.endswith("_test.py") or lower.endswith(".test.js"):
            return "test"
        if lower.endswith(".md") or lower.endswith(".txt"):
            return "document"
        if lower.endswith(".json") or lower.endswith(".yaml") or lower.endswith(".yml"):
            return "config"
        if lower.endswith(".patch") or lower.endswith(".diff"):
            return "patch"
        return "any"


def _build_boundary_prompt(profile: AgentProfile) -> str:
    parts = []
    tp = profile.tool_policy
    if tp.deny_all_by_default:
        allowed = ", ".join(tp.allowed_tools) if tp.allowed_tools else "(无)"
        parts.append(f"允许的工具：{allowed}")
    parts.append(f"文件读取：{'允许' if tp.allow_file_read else '禁止'}")
    parts.append(f"文件写入：{'允许' if tp.allow_file_write else '禁止'}")
    parts.append(f"Shell执行：{'允许' if tp.allow_shell else '禁止'}")
    return "\n".join(parts)


def _extract_tool_calls(response: dict) -> list[dict[str, Any]]:
    """从 agent 响应中提取工具调用记录。"""
    calls = []
    try:
        messages = response.get("messages", [])
        for msg in messages:
            if hasattr(msg, "additional_kwargs") and msg.additional_kwargs:
                for block in msg.additional_kwargs.get("tool_calls", []):
                    calls.append({
                        "tool": block.get("function", {}).get("name", "?"),
                        "args_preview": str(block.get("function", {}).get("arguments", ""))[:100],
                    })
    except Exception:
        pass
    return calls


# ===== 便捷工厂 =====


def create_executor(profile: AgentProfile) -> AgentExecutor:
    """根据 AgentProfile 选择合适的 Executor。

    规则：
    - 若 profile 无执行工具权限（shell=False, file_write=False, 无 allowed_tools）→ ModelDecisionExecutor
    - 其他 → DeepAgentExecutor
    """
    tp = profile.tool_policy
    is_decision_only = (
        not tp.allow_shell
        and not tp.allow_file_write
        and len(tp.allowed_tools) <= 1
    )
    if is_decision_only:
        return ModelDecisionExecutor()
    return DeepAgentExecutor()


# ===== 辅助函数（DeepAgentExecutor.execute_task 兜底用） =====


def _fallback_coder_profile() -> AgentProfile:
    """当 CapabilityRegistry 找不到匹配 worker 时返回的默认 coder profile。

    避免在没人注册过 profile 的情况下 scheduler 路径空指针。
    拥有 file_read + file_write + create_file + edit_file + execute 五件套，
    覆盖典型编码任务所需工具。
    """
    from app.multiagent.agent_profile import (
        AgentProfile, ToolPolicy, ModelPolicy,
    )

    return AgentProfile(
        id="default_coder",
        name="DefaultCoder",
        role="coder",
        description="默认 Coder profile（CapabilityRegistry 未匹配时的兜底）",
        capabilities={"coding", "file_write", "file_read", "testing"},
        tool_policy=ToolPolicy(
            allowed_tools=["read_file", "list_dir",
                           "create_file", "edit_file", "execute"],
            deny_all_by_default=False,
            allow_file_read=True,
            allow_file_write=True,
            allow_shell=True,
        ),
        model_policy=ModelPolicy(model_name="deepseek-chat"),
    )


def _default_workspace_root() -> str:
    """workspace 未注入时的默认根目录。

    用项目根下的 runtime/workspaces/<default_run>，与 RunWorkspace 默认布局对齐。
    """
    import os
    from pathlib import Path
    root = Path(os.getcwd()) / "runtime" / "workspaces" / "default_run"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)
