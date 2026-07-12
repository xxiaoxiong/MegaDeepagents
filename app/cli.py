"""CLI 入口：支持单轮对话、任务管理、Skills 和记忆查询。"""

import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from typer import Typer, Argument, Option

from app.core.config import settings
from app.core.logging import logger
from app.task.service import get_task_service
from app.memory.hot_memory import get_hot_memory
from app.memory.fts import search_fts
from app.skills.loader import get_skill_loader
from app.tools.registry import ToolRegistry
from app.skills.metadata import list_skills as meta_list_skills, register_skill, get_skill

app = Typer(name="general-agent-framework", help="通用任务型智能体")
console = Console()

# 子命令组
skills_app = Typer(name="skills", help="Skill 管理")
memory_app = Typer(name="memory", help="记忆管理")

app.add_typer(skills_app)
app.add_typer(memory_app)

team_app = Typer(name="team", help="多智能体团队任务管理")
app.add_typer(team_app)


@team_app.command("run")
def run_team(
    goal: str = Argument(..., help="任务目标"),
    team: str = Option("software_dev_team", "--team", "-t", help="团队模板名"),
    max_rounds: int = Option(10, "--max-rounds", "-m", help="最大轮次"),
    review_required: bool = Option(True, "--review/--no-review", help="是否需要评审"),
    workspace: str = Option("", "--workspace", "-w", help="产出文件目录（默认 runtime/workspaces/）"),
    legacy: bool = Option(False, "--legacy", help="使用旧 TeamRunner 主循环（不走 Phase Two 流程）"),
):
    """运行多 Agent 团队任务。

    默认走 Phase Two 编排（TaskGraph + DeepAgentExecutor + 真实文件产出）。
    加 --legacy 回退旧 TeamRunner。
    """
    from app.core.observability import init_observability
    from app.multiagent.orchestrator import run_orchestrated
    from app.multiagent.executor import DeepAgentExecutor
    from app.multiagent.verifier import Verifier, LLMRubricVerifier
    from app.multiagent.planner import plan_with_llm
    from app.multiagent.artifact import ArtifactStore, ArtifactType, compute_content_hash
    from app.multiagent.run_workspace import RunWorkspace, create_run_workspace
    from app.multiagent.task_graph import TaskNode, TaskNodeStatus
    import uuid, os
    from pathlib import Path

    init_observability(component="cli")

    if legacy:
        from app.multiagent.team_runner import run_team_task
        with console.status("[bold green]Team running (legacy)..."):
            result = run_team_task(
                goal=goal, team_name=team,
                max_rounds=max_rounds, review_required=review_required,
            )
        console.print(f"[bold]Status:[/bold] {result.status}")
        console.print(f"[bold]Phase:[/bold] {result.phase}")
        console.print(f"[bold]Rounds:[/bold] {result.total_rounds}")
        console.print(f"[bold]Reason:[/bold] {result.termination_reason}")
        if result.final_output:
            console.print(f"[bold]Final:[/bold]\n{result.final_output[:500]}")
        return

    # ---- Phase Two 路径 ----
    run_id = "cli_" + uuid.uuid4().hex[:12]
    base = workspace or os.path.join(os.getcwd(), "runtime", "workspaces")
    ws = create_run_workspace(run_id, base_root=base)
    console.print(f"[dim]Workspace: {ws.workspace_root}[/dim]")

    # executor + verifier
    executor = DeepAgentExecutor()
    verifier = Verifier(llm_rubric=LLMRubricVerifier(model_available=False))

    # planner: 用真实 LLM 生成 TaskGraph（不假，走 build_model）
    planner = lambda g, c: plan_with_llm(g, context=c)

    with console.status("[bold green]Phase Two team running..."):
        result = run_orchestrated(
            goal=goal,
            mode_override="full_multi",
            planner=planner,
            executor=executor,
            verifier=verifier,
        )

    # ---- 输出 ----
    console.print(f"[bold]Status:[/bold] {result.status}")
    console.print(f"[bold]Mode:[/bold] {result.mode}")
    console.print(f"[bold]Verdict:[/bold] {result.verification_verdict}")
    console.print(f"[bold]Tasks:[/bold] {result.total_tasks} total, "
                  f"{result.succeeded_tasks} succeeded, {result.failed_tasks} failed")
    if result.summary:
        console.print(f"[bold]Summary:[/bold] {result.summary[:300]}")

    # 列出 workspace 中实际产出的文件
    output_files = list(Path(ws.workspace_root).rglob("*"))
    real_files = [f for f in output_files if f.is_file()]
    if real_files:
        console.print(f"\n[bold green]产出文件 ({len(real_files)}):[/bold green]")
        for f in sorted(real_files):
            rel = os.path.relpath(str(f), ws.workspace_root)
            size = f.stat().st_size
            console.print(f"  [cyan]{rel}[/cyan] ({size} bytes)")
        console.print(f"\nWorkspace 根目录: [yellow]{ws.workspace_root}[/yellow]")
    else:
        console.print("[yellow]未产生磁盘文件（Agent 可能只输出了文本）。[/yellow]")
    if result.error:
        console.print(f"[red]Error:[/red] {result.error}")


@team_app.command("list")
def team_list():
    """列出可用团队模板。"""
    from app.multiagent.default_teams import list_teams
    ts = list_teams()
    if not ts:
        console.print("暂无团队模板。")
        return
    table = Table(title="Available Teams")
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="green")
    for name in ts:
        from app.multiagent.default_teams import get_team
        spec = get_team(name)
        table.add_row(name, spec.description if spec else "")
    console.print(table)


@app.command()
def run(
    message: str = Argument(..., help="任务内容"),
    thread_id: str = Option("cli-default", "--thread-id", "-t", help="会话线程 ID"),
    auto_approve: bool = Option(True, "--auto-approve/--no-auto-approve", "-y", help="自动审批"),
):
    """运行一个任务。"""
    from app.task.runner import TaskRunner

    task_service = get_task_service()
    runner = TaskRunner(task_service, thread_id=thread_id, auto_approve=auto_approve)
    with console.status("[bold green]Agent running..."):
        result = runner.run(message)
    console.print(f"[bold]Status:[/bold] {result.get('status')}")
    if result.get("content"):
        console.print(f"[bold]Result:[/bold]\n{result['content']}")
    if result.get("error"):
        console.print(f"[bold red]Error:[/bold red] {result['error']}")


@app.command("task-list")
def task_list(limit: int = Option(20, "--limit", "-n", help="显示条数")):
    """列出任务。"""
    task_service = get_task_service()
    tasks = task_service.list_tasks(limit)
    if not tasks:
        console.print("暂无任务。")
        return

    table = Table(title="Tasks")
    table.add_column("ID", style="cyan")
    table.add_column("Status", style="magenta")
    table.add_column("User Input", style="green")
    table.add_column("Updated", style="yellow")
    for t in tasks:
        table.add_row(
            t.task_id,
            t.status.value,
            t.user_input[:40] + "..." if len(t.user_input) > 40 else t.user_input,
            t.updated_at.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


@app.command("task-show")
def task_show(task_id: str = Argument(..., help="任务 ID")):
    """查看任务详情。"""
    task_service = get_task_service()
    task = task_service.store.get_task_full(task_id)
    if not task:
        console.print(f"[red]Task {task_id} not found.[/red]")
        return

    console.print(f"[bold]Task ID:[/bold] {task.task_id}")
    console.print(f"[bold]Status:[/bold] {task.status.value}")
    console.print(f"[bold]User Input:[/bold] {task.user_input}")
    console.print(f"[bold]Final Answer:[/bold] {task.final_answer}")
    console.print(f"[bold]Error:[/bold] {task.error_message}")
    if task.artifacts:
        console.print(f"[bold]Artifacts:[/bold]")
        for a in task.artifacts:
            console.print(f"  - {a.name} ({a.path})")


@skills_app.command("list")
def skills_list():
    """列出 Skills。"""
    loader = get_skill_loader()
    skills = loader.scan()
    metas = meta_list_skills()
    meta_map = {m["name"]: m for m in metas}
    if not skills:
        console.print("暂无 Skills。")
        return

    table = Table(title="Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="green")
    table.add_column("CreatedBy", style="yellow")
    table.add_column("State", style="magenta")
    table.add_column("Pinned", style="blue")
    for s in skills:
        m = meta_map.get(s.name, {})
        table.add_row(
            s.name,
            s.description[:50],
            m.get("created_by", "user"),
            m.get("state", "active"),
            "Y" if m.get("pinned") else "N",
        )
    console.print(table)


@skills_app.command("show")
def skills_show(name: str = Argument(..., help="Skill 名称")):
    """查看 Skill 详情。"""
    loader = get_skill_loader()
    info = loader.get(name)
    meta = get_skill(name)
    if info:
        console.print(f"[bold]Name:[/bold] {info.name}")
        console.print(f"[bold]Description:[/bold] {info.description}")
        console.print(f"[bold]Path:[/bold] {info.path}")
    if meta:
        console.print(f"[bold]CreatedBy:[/bold] {meta.get('created_by')}")
        console.print(f"[bold]State:[/bold] {meta.get('state')}")
        console.print(f"[bold]Version:[/bold] {meta.get('version')}")
        console.print(f"[bold]Pinned:[/bold] {meta.get('pinned')}")
        console.print(f"[bold]ContentHash:[/bold] {meta.get('content_hash')}")
        console.print(f"[bold]LastUsed:[/bold] {meta.get('last_used_at')}")


@skills_app.command("scan")
def skills_scan():
    """扫描并注册 Skills。"""
    loader = get_skill_loader()
    skills = loader.scan()
    for s in skills:
        frontmatter = s.frontmatter or {}
        created_by = frontmatter.get("created_by", "user")
        source = frontmatter.get("source", "local")
        state = frontmatter.get("state", "active")
        pinned = frontmatter.get("pinned", False)
        meta = register_skill(
            name=s.name,
            path=Path(s.path),
            description=s.description,
            created_by=created_by,
            source=source,
            state=state,
            pinned=pinned,
        )
        console.print(f"Registered: {s.name} -> {meta['id']}")


@memory_app.command("show")
def memory_show_cmd():
    """查看热记忆。"""
    hot = get_hot_memory()
    console.print("[bold]===== MEMORY.md =====[/bold]")
    console.print(hot.read_memory())
    console.print("\n[bold]===== USER.md =====[/bold]")
    console.print(hot.read_user())


@memory_app.command("search")
def memory_search_cmd(query: str = Argument(..., help="检索关键词")):
    """搜索历史记忆。"""
    try:
        results = search_fts(query, limit=5)
    except Exception as exc:
        console.print(f"[red]Search error: {exc}[/red]")
        return

    if not results:
        console.print("未找到相关记录。")
        return

    console.print(f"[bold]Found {len(results)} results:[/bold]")
    for r in results:
        console.print(f"[cyan][{r.get('role', '?')}][/cyan] {r.get('content', '')[:200]}")


@app.command()
def tools_list():
    """列出工具集。"""
    registry = ToolRegistry()
    registry.register_all()
    toolsets = registry.list_toolsets()
    table = Table(title="Tool Sets")
    table.add_column("Toolset", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Tools", style="yellow")
    for name, info in toolsets.items():
        table.add_row(
            name,
            str(info["enabled"]),
            ", ".join(info["tools"]) if info["tools"] else "（智能体内置）",
        )
    console.print(table)


@app.command("config-show")
def config_show_cmd():
    """显示当前配置摘要。"""
    console.print(settings.summary())


if __name__ == "__main__":
    app()
