"""Structured Planner — 将用户目标分解为 TaskGraph。

V3 planner contract：
Planner 输出不能再只是自然语言 plan。
必须返回符合 Schema 的任务图建议，包括：Task ID / 目标 / 依赖 / 所需能力 / 输入 /
输出契约 / 验收条件 / 预算建议 / 是否允许并行。

对 Planner 输出执行：
1. Pydantic 校验 → 2. DAG 校验 → 3. 能力存在性校验 → 4. 输出契约校验
5. 失败时结构化重试 → 6. 多次失败后进入人工或降级策略
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.core.logging import logger
from app.multiagent.task_graph import (
    TaskGraph,
    TaskNode,
    TaskNodeStatus,
    TaskBudget,
    OutputContract,
    capability_timeout,
)


class PlanValidationError(Exception):
    """Planner 输出校验失败。"""
    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


# Per-capability execution timeouts now live in ``task_graph.CAPABILITY_TIMEOUTS``
# so that ``add_repair_task`` and ``ParallelTeamScheduler`` can share the same
# map.  See ``capability_timeout()`` for the lookup helper.


def _llm_plan_to_taskgraph(json_output: dict | str, goal: str) -> TaskGraph:
    """将 LLM 的 JSON 输出解析为 TaskGraph。

    LLM 输出期望格式：
    ```json
    {
      "tasks": [
        {
          "id": "task_1",
          "title": "设计 API",
          "objective": "设计 REST API 接口规范",
          "description": "...",
          "dependencies": [],
          "required_capabilities": ["planning"],
          "output_artifact_type": "document",
          "acceptance_criteria": ["包含至少 3 个端点"],
          "priority": 10,
          "allow_parallel": true,
          "requires_input_artifact_ids": []
        },
        ...
      ]
    }
    ```
    """
    if isinstance(json_output, str):
        parsed = json.loads(json_output)
    else:
        parsed = json_output

    tasks_raw = parsed.get("tasks", []) if isinstance(parsed, dict) else parsed
    if not tasks_raw:
        raise PlanValidationError("LLM 输出不包含 tasks 列表")

    # 收集所有 task ids
    all_ids = set()
    for t in tasks_raw:
        tid = t.get("id", "")
        if not tid:
            raise PlanValidationError("task 缺少 id 字段")
        if tid in all_ids:
            raise PlanValidationError(f"重复的 task id: {tid}")
        all_ids.add(tid)

    graph = TaskGraph(root_task_id="", nodes={})

    for t in tasks_raw:
        dep_ids = t.get("dependencies", [])
        # 验证依赖存在
        for d in dep_ids:
            if d not in all_ids and d not in graph.nodes:
                raise PlanValidationError(f"task {t['id']} 依赖不存在的 task {d}")

        caps = t.get("required_capabilities", []) or ["default"]
        allow_parallel = t.get("allow_parallel", True)

        # 保险：LLM 偶尔会把多个"主角色能力"（planning/research/coding/
        # testing/reviewing/summarization）合并到同一 task，导致找不到
        # 同时具备这些角色的 worker。这里只保留**第一个主角色**，再加
        # 上其它"工具能力"（file_read/file_write/shell_execute/web_research/
        # mcp_access），把任务规格收敛到 team 中真实可分配的形态。
        # 同时过滤掉 LLM 偶尔混入的未知标签（如把 output_artifact_type
        # 的 "config" 误当作能力声明），避免匹配不到任何 worker。
        PRIMARY_CAPS = {
            "planning", "research", "coding", "testing",
            "reviewing", "summarization",
        }
        TOOL_CAPS = {
            "file_read", "file_write", "shell_execute",
            "web_research", "mcp_access", "default",
        }
        KNOWN_CAPS = PRIMARY_CAPS | TOOL_CAPS
        primary_caps = [c for c in caps if c in PRIMARY_CAPS]
        tool_caps = [c for c in caps if c in TOOL_CAPS]
        unknown_caps = [c for c in caps if c not in KNOWN_CAPS]
        if unknown_caps:
            logger.warning(
                f"[Planner] task {t.get('id')} 声明了未知能力 {unknown_caps}，已过滤。"
            )
        if len(primary_caps) > 1:
            keep_primary = primary_caps[0]
            logger.warning(
                f"[Planner] task {t.get('id')} 声明了多个主角色能力 {primary_caps}，"
                f"裁剪为仅保留 {keep_primary!r}（工具能力 {tool_caps} 仍保留）。"
                f"如需多角色，应拆分为多个 task 通过 dependencies 串联。"
            )
            caps = [keep_primary] + tool_caps

        # 启发式纠错：LLM 对"构建项目"类目标常把所有子任务标成 ["planning"]
        # 或干脆返回空 required_capabilities（fallback 为 ["default"]），导致：
        # 1. TeamBuilder 只生成 Planner agent、Planner 凭 file_write 自己实现
        #    一切（用户症状：只有 planner 在干活）。
        # 2. capability_timeout() 返回 0.0 → 任务用 600s 全局默认超时 → coding
        #    task 在 600s 被杀（run_a3a9f8e5f5004e21 task_2: 54 个 create_file
        #    后超时，artifact 不完整 → repair 循环）。
        #
        # 修复：对 planning-only / default / 空 caps 的 task 扫描 title+objective+
        # description 文本：命中测试关键词→改写为 testing；命中实现关键词→改写为
        # coding；让 Coder/Tester 真正被生成与调度，且拿到正确的 per-capability
        # 超时（coding→900s, planning→1200s, testing→300s）。
        # 纯规划/设计/架构类保留 planning。英文关键词用词边界避免 "latest"→test
        # 之类的误判。
        needs_heuristic = (
            not caps
            or caps[0] in ("planning", "default")
        ) and not any(c in PRIMARY_CAPS for c in caps[1:])
        if needs_heuristic:
            text = " ".join([
                str(t.get("title", "")),
                str(t.get("objective", "")),
                str(t.get("description", "")),
            ])
            text_lower = text.lower()
            # 设计/架构/规划类关键词优先：即使文本提到"前端/后端"（如"设计前后端
            # 架构"），也应保留 planning，避免把架构设计任务误判为 coding。这优先于
            # impl/test 命中——架构 task 的产物是设计文档，不是代码。
            design_hit = any(kw in text for kw in (
                "设计", "架构", "规划", "方案", "技术选型", "调研",
            )) or any(
                re.search(rf"\b{kw}\b", text_lower)
                for kw in ("design", "architecture", "blueprint", "plan", "spec")
            )
            test_hit = any(kw in text for kw in ("测试", "验收测试")) or any(
                re.search(rf"\b{kw}\b", text_lower)
                for kw in ("test", "pytest", "unittest")
            )
            impl_hit = any(kw in text for kw in (
                "实现", "编写", "创建", "构建", "开发", "前端", "后端",
                "组件", "页面", "接口", "数据库",
            )) or any(
                re.search(rf"\b{kw}\b", text_lower)
                for kw in ("develop", "implement", "build", "code", "api",
                           "service", "server", "client")
            )
            if design_hit:
                # 纯设计/架构 task：保留/设为 planning，不改写为 coding
                if not caps or caps[0] == "default":
                    caps = ["planning"] + [c for c in caps[1:] if c in TOOL_CAPS]
                    logger.warning(
                        f"[Planner] task {t.get('id')} 无主角色能力但文本含设计关键词，"
                        f"设为 planning 以便 Planner 接管并拿到 1200s 超时。"
                    )
                # else: already ["planning"], keep as-is
            elif test_hit:
                caps = ["testing"] + [c for c in caps[1:] if c in TOOL_CAPS]
                logger.warning(
                    f"[Planner] task {t.get('id')} 声明 {caps[0] if caps else '空'} 但文本含测试关键词，"
                    f"改写主角色能力为 testing 以便 Tester 接管。"
                )
            elif impl_hit:
                caps = ["coding"] + [c for c in caps[1:] if c in TOOL_CAPS]
                logger.warning(
                    f"[Planner] task {t.get('id')} 声明 {caps[0] if caps else '空'} 但文本含实现关键词，"
                    f"改写主角色能力为 coding 以便 Coder 接管。"
                )
            elif not caps or caps[0] == "default":
                # 无任何命中且 caps 为空/default：默认设为 planning，确保
                # capability_timeout() 返回 1200s 而非 0.0→600s 全局默认。
                caps = ["planning"] + [c for c in caps[1:] if c in TOOL_CAPS]
                logger.warning(
                    f"[Planner] task {t.get('id')} 无主角色能力且无关键词命中，"
                    f"默认设为 planning。"
                )

        # LLM gateways intermittently 429 or time out; a 2-attempt budget
        # turns a single transient failure into a permanent task failure.
        # 4 attempts gives the RATE_LIMITED backoff (15/30/60s) room to
        # outlast the throttle window.  The LLM frequently ignores the
        # prompt's "默认 4" hint and emits max_attempts=2; ``max(..., 4)``
        # enforces a sane floor regardless of what the LLM outputs.
        # NB: set on the TaskNode directly — transactional_task_service,
        # executor, and parallel_scheduler all read ``node.max_attempts``
        # (NOT ``node.budget.max_attempts``).  Setting only on the budget
        # (as the previous code did) was dead code; run_2a438328372441d8
        # showed every task stuck at attempt 1/2 despite the planner
        # claiming a default of 4.
        enforced_max_attempts = max(int(t.get("max_attempts", 4) or 4), 4)

        # Per-capability execution timeout.  A single global timeout cannot
        # fit both planning (deep LLM thinking, ~5-8 min) and testing (fast
        # shell runs, ~2-3 min).  The planner picks the timeout from the
        # task's primary capability; 0.0 means "use scheduler default".
        # 300s was too short for planning tasks — run_2a438328372441d8 T01
        # (a planning task) was killed at 300s before it could finish, then
        # its retry hit a 429 and permanently failed the run.
        timeout_seconds = capability_timeout(caps)

        # 默认验收条件：LLM 经常返回空 acceptance_criteria（run_3fb3c2572f1348b0
        # task_4 测试任务的 contract 全空），导致 Verifier 只能用默认 rubric 评估，
        # LLM rubric 会幻觉出"所有测试用例必须通过且提供执行结果证据"这类契约里
        # 没有的要求，然后因"仅提供了测试配置文件、无执行结果"判 REPLAN/REPAIR，
        # 把测试任务打入无限修复。
        #
        # 修复：对空 contract 的 task 按主角色能力注入合理的语义验收条件，让
        # LLM rubric 有具体维度可评，而不是自行发明标准。
        acceptance_criteria = list(t.get("acceptance_criteria") or [])
        if not acceptance_criteria:
            primary_cap = caps[0] if caps else "default"
            if primary_cap == "testing":
                acceptance_criteria = [
                    "测试文件已创建且包含可执行的测试用例",
                    "测试覆盖核心功能场景（认证、增删改查、异常处理等）",
                    "测试代码结构清晰、断言明确",
                ]
                logger.warning(
                    f"[Planner] task {t.get('id')} 为 testing 任务但无验收条件，"
                    f"注入默认测试验收条件。"
                )
            elif primary_cap == "coding":
                acceptance_criteria = [
                    "实现代码已创建且非空",
                    "代码结构与架构设计一致",
                    "核心功能逻辑已实现",
                ]
                logger.warning(
                    f"[Planner] task {t.get('id')} 为 coding 任务但无验收条件，"
                    f"注入默认实现验收条件。"
                )
            elif primary_cap == "planning":
                acceptance_criteria = [
                    "设计文档已创建且涵盖架构、模块划分、技术选型",
                    "文档内容完整、结构清晰",
                ]
                logger.warning(
                    f"[Planner] task {t.get('id')} 为 planning 任务但无验收条件，"
                    f"注入默认设计验收条件。"
                )

        node = TaskNode(
            id=t["id"],
            title=t.get("title", t["id"]),
            objective=t.get("objective", t["id"]),
            description=t.get("description", ""),
            status=TaskNodeStatus.PENDING,
            dependencies=dep_ids,
            required_capabilities=caps,
            input_artifact_ids=t.get("requires_input_artifact_ids", []),
            output_contract=OutputContract(
                artifact_type=t.get("output_artifact_type", "any"),
                description=t.get("objective", t["id"]),
                acceptance_criteria=acceptance_criteria,
                allow_parallel=allow_parallel,
            ),
            priority=t.get("priority", 5),
            max_attempts=enforced_max_attempts,
            budget=TaskBudget(
                max_attempts=enforced_max_attempts,
                max_seconds=timeout_seconds,
            ),
        )
        graph.add_node(node)

    # 设置 root_task_id：无依赖且 priority 最高的 task
    no_dep_tasks = [n for n in graph.nodes.values() if not n.dependencies]
    if no_dep_tasks:
        root = max(no_dep_tasks, key=lambda n: n.priority)
        graph.root_task_id = root.id

    return graph


def validate_plan(graph: TaskGraph) -> None:
    """对 TaskGraph 做多层校验。

    1. Pydantic 校验（TaskNode 构造时已做）
    2. DAG 校验（环检测）
    3. 能力存在性校验（不抛，只记 WARNING）
    4. 输出契约完整性校验
    """
    errors: list[str] = []

    # 2. DAG 校验
    if graph.has_cycle():
        raise PlanValidationError("TaskGraph 存在环，拒绝接受", ["cycle detected"])

    # 3. 能力存在性校验
    known_capabilities = {
        "planning", "research", "coding", "testing", "reviewing",
        "summarization", "file_read", "file_write", "shell_execute",
        "web_research", "mcp_access", "default",
    }
    for node in graph.nodes.values():
        for cap in node.required_capabilities:
            if cap not in known_capabilities:
                logger.warning(
                    f"[Planner] task {node.id} 声明未知能力 {cap!r}"
                )

    # 4. 输出契约校验
    for node in graph.nodes.values():
        if not node.output_contract.acceptance_criteria and not node.output_contract.required_artifacts:
            logger.warning(
                f"[Planner] task {node.id} 无验收条件和输出 Artifact 要求"
            )

    if errors:
        raise PlanValidationError("Plan validation failed", errors)


def plan_with_llm(
    goal: str,
    context: str = "",
    max_retries: int = 2,
    llm: Any | None = None,
) -> TaskGraph:
    """用 LLM 将目标分解为结构化 TaskGraph。

    Args:
        goal: 用户目标
        context: 额外上下文（项目结构、代码文件等）
        max_retries: 解析失败时的重试次数
        llm: 外部 LLM 实例（用于测试注入）。None 则调用 build_model()

    Returns:
        TaskGraph: 结构化任务图

    Raises:
        PlanValidationError: 所有重试均失败
    """
    if llm is None:
        from app.llm_factory import build_model
        llm = build_model()

    system = (
        "你是一个专业的任务规划师。你的职责是分析用户目标并将其拆解为"
        "结构化的任务依赖图。\n\n"
        "输出必须是一个 JSON 对象，包含 'tasks' 数组。每个 task 包含：\n"
        "- id: 唯一标识符\n"
        "- title: 简短标题\n"
        "- objective: 具体目标\n"
        "- description: 详细描述\n"
        "- dependencies: 前置依赖的 task id 列表\n"
        "- required_capabilities: 所需能力列表，必须从以下选取："
        "planning/research/coding/testing/reviewing/summarization/"
        "file_read/file_write/shell_execute/web_research/mcp_access\n"
        "- output_artifact_type: 产出物类型（code/test/document/patch/report/config/any）\n"
        "- acceptance_criteria: 验收条件列表\n"
        "- priority: 优先级（0-10，越高越优先）\n"
        "- allow_parallel: 布尔值，是否允许与其他任务并行\n"
        "- max_attempts: 最大尝试次数（默认 4；LLM 偶发 429/超时时需要更多重试机会）\n\n"
        "规则：\n"
        "1. 无依赖的任务可以并行执行\n"
        "2. 一个 task 产出的 artifact 必须能被子 task 引用\n"
        "3. 评审任务应依赖对应的实现任务\n"
        "4. test 任务应依赖 coding 任务\n"
        "5. 如果目标包含研究任务，research 应排在最前面\n"
        "6. 输出必须仅包含 JSON\n"
        "7. 每个 task 的 required_capabilities 必须只包含**唯一一个主角色能力**"
        "（planning/research/coding/testing/reviewing/summarization 之一）；"
        "不要把多个主角色能力合并到同一 task，需要多个角色时请拆分为多个 task，"
        "通过 dependencies 串联。\n"
        "8. file_read/file_write/shell_execute/web_research/mcp_access 是工具能力，"
        "可以随主角色能力附带声明；常见的附着方式：\n"
        "   - planning 任务: 仅 ['planning']\n"
        "   - research 任务: ['research', 'web_research']\n"
        "   - coding 任务: ['coding', 'file_read', 'file_write', 'shell_execute']\n"
        "   - testing 任务: ['testing', 'file_read', 'shell_execute']\n"
        "   - reviewing 任务: ['reviewing', 'file_read']\n"
        "   - summarization 任务: ['summarization', 'file_read', 'file_write']\n"
        "9. **能力与角色必须匹配**（关键）：\n"
        "   - 只有产出规划/架构设计/方案文档的 task 才用 'planning'；\n"
        "   - 任何涉及编写代码、创建代码文件、实现功能的 task 必须用 'coding'；\n"
        "   - 任何涉及编写或运行测试的 task 必须用 'testing'；\n"
        "   - 不要让 'planning' 任务承担代码实现工作。\n"
        "10. **构建项目类目标必须多角色协作**：不得把整个项目塞进一个 planning 任务。"
        "应拆为：planning(架构设计) → coding(各模块实现) → testing(测试)，"
        "通过 dependencies 串联，让 Coder/Tester 等不同角色 agent 真正参与。\n"
        "11. 示例（目标“构建一个前后端项目”）：\n"
        "   - task_1: 架构设计, required_capabilities=['planning'], 依赖[]\n"
        "   - task_2: 实现前端, required_capabilities=['coding','file_read','file_write','shell_execute'], 依赖['task_1']\n"
        "   - task_3: 实现后端 API, required_capabilities=['coding','file_read','file_write','shell_execute'], 依赖['task_1']\n"
        "   - task_4: 编写并运行测试, required_capabilities=['testing','file_read','shell_execute'], 依赖['task_2','task_3']"
    )

    prompt = f"## 用户目标\n{goal}\n\n## 额外上下文\n{context or '(无)'}"

    last_error: str | None = None
    for attempt in range(1, max_retries + 2):
        try:
            try:
                json_llm = llm.bind(response_format={"type": "json_object"})
            except Exception:
                json_llm = llm

            response = json_llm.invoke([
                ("system", system),
                ("user", prompt),
            ])
            text = getattr(response, "content", str(response))
            if isinstance(text, list):
                text = json.dumps(text, ensure_ascii=False)

            # Debug: log the raw LLM response to diagnose parsing failures
            logger.info(
                f"[Planner] LLM response (attempt {attempt}, len={len(text) if isinstance(text, str) else 'n/a'}): "
                f"{(text[:500] if isinstance(text, str) else str(text)[:500])}"
            )

            # 先解析 JSON
            parsed = json.loads(text) if isinstance(text, str) else text

            graph = _llm_plan_to_taskgraph(parsed, goal)
            validate_plan(graph)

            logger.info(
                f"[Planner] 计划生成成功: {len(graph.nodes)} tasks, "
                f"version={graph.version}, has_cycle={graph.has_cycle()}"
            )
            return graph

        except json.JSONDecodeError as exc:
            last_error = f"JSON 解析失败: {exc}"
            logger.warning(f"[Planner] attempt {attempt}: {last_error}")
            continue
        except PlanValidationError as exc:
            last_error = f"校验收失败: {exc}"
            logger.warning(f"[Planner] attempt {attempt}: {last_error}")
            continue
        except Exception as exc:
            last_error = f"LLM 调用异常: {exc}"
            logger.warning(f"[Planner] attempt {attempt}: {last_error}")
            continue

    raise PlanValidationError(
        f"Planner 在 {max_retries + 1} 次尝试后全部失败: {last_error}",
    )


# ===== 降级策略 =====

def build_fallback_plan(goal: str) -> TaskGraph:
    """当 LLM Planner 多次失败时，生成一个基础的两步计划。

    降级策略：plan → execute
    """
    graph = TaskGraph(root_task_id="plan")
    plan_node = TaskNode(
        id="plan",
        title="规划",
        objective=f"规划如何实现: {goal[:100]}",
        status=TaskNodeStatus.PENDING,
        required_capabilities=["planning"],
        output_contract=OutputContract(
            artifact_type="plan",
            description="规划文档",
        ),
    )
    execute_node = TaskNode(
        id="execute",
        title="执行",
        objective=f"实现目标: {goal[:100]}",
        status=TaskNodeStatus.PENDING,
        dependencies=["plan"],
        required_capabilities=["coding", "testing"],
        output_contract=OutputContract(
            artifact_type="code",
            description="实现产物",
            acceptance_criteria=["功能实现并可通过测试"],
        ),
    )
    graph.add_node(plan_node)
    graph.add_node(execute_node)
    return graph
