"""Structured Planner — 将用户目标分解为 TaskGraph。

docs/upgradePhaseTwo.md §六：
Planner 输出不能再只是自然语言 plan。
必须返回符合 Schema 的任务图建议，包括：Task ID / 目标 / 依赖 / 所需能力 / 输入 /
输出契约 / 验收条件 / 预算建议 / 是否允许并行。

对 Planner 输出执行：
1. Pydantic 校验 → 2. DAG 校验 → 3. 能力存在性校验 → 4. 输出契约校验
5. 失败时结构化重试 → 6. 多次失败后进入人工或降级策略
"""
from __future__ import annotations

import json
from typing import Any

from app.core.logging import logger
from app.multiagent.task_graph import (
    TaskGraph,
    TaskNode,
    TaskNodeStatus,
    TaskBudget,
    OutputContract,
)


class PlanValidationError(Exception):
    """Planner 输出校验失败。"""
    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


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
                acceptance_criteria=t.get("acceptance_criteria", []),
                allow_parallel=allow_parallel,
            ),
            priority=t.get("priority", 5),
            budget=TaskBudget(max_attempts=t.get("max_attempts", 2)),
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
        "- required_capabilities: 所需能力列表，从以下选取："
        "planning/research/coding/testing/reviewing/summarization/"
        "file_read/file_write/shell_execute/web_research/mcp_access\n"
        "- output_artifact_type: 产出物类型（code/test/document/patch/report/config/any）\n"
        "- acceptance_criteria: 验收条件列表\n"
        "- priority: 优先级（0-10，越高越优先）\n"
        "- allow_parallel: 布尔值，是否允许与其他任务并行\n"
        "- max_attempts: 最大尝试次数（默认 2）\n\n"
        "规则：\n"
        "1. 无依赖的任务可以并行执行\n"
        "2. 一个 task 产出的 artifact 必须能被子 task 引用\n"
        "3. 评审任务应依赖对应的实现任务\n"
        "4. test 任务应依赖 coding 任务\n"
        "5. 如果目标包含研究任务，research 应排在最前面\n"
        "6. 输出必须仅包含 JSON"
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
