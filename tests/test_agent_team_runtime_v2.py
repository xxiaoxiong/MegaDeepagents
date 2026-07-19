"""Production invariants for the TASK_TEAM v2 runtime."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.multiagent.agent_profile import get_capability_registry
from app.multiagent.agent_registry import get_agent_registry
from app.multiagent.artifact import ArtifactStatus, ArtifactStore, ArtifactType
from app.multiagent.dynamic_team import DynamicTeamManager, TeamBudget
from app.multiagent.git_workspace import (
    AgentWorktreeManager,
    GitIntegrationManager,
    MergeQueueItem,
    RepositoryWorkspaceManager,
)
from app.multiagent.permission import (
    PermissionBroker,
    PermissionDecision,
    PermissionKind,
    PermissionPolicy,
    PermissionRequired,
)
from app.multiagent.lifecycle_hooks import (
    HookResult, LifecycleEvent, get_lifecycle_hook_engine,
)
from app.multiagent.phase_g_store import get_agent_run_history, make_run_event_id
from app.multiagent.plan_approval import PlanApprovalService, PlanStatus, TeammatePlan
from app.multiagent.shell_policy import (
    CommandCategory,
    ShellCommandRunner,
    ShellPolicyEngine,
)
from app.multiagent.task_board import BoardTaskStatus, TaskBoard, get_task_board
from app.multiagent.task_graph import TaskGraph, TaskNode
from app.multiagent.teammate_session import (
    TeammateCommandQueue,
    TeammateCommandType,
    TeammateLifecycle,
    get_teammate_supervisor,
    reset_teammate_supervisor,
)
from app.multiagent.transactional_task_service import TransactionalTaskService
from app.multiagent.tool_runtime import (
    ToolInvocation, ToolInvocationStatus, ToolSideEffectJournal,
)
from app.multiagent.verifier import LLMRubricVerifier, Verdict


def _agent(run_id: str = "run_v2", profile_id: str = "coder"):
    return get_agent_registry().create_agent(
        profile_id=profile_id, name="worker", role="coder", team_id="team",
        run_id=run_id, capabilities=["coding", "testing"],
        workspace_root="/tmp/workspace-v2",
    )


def _git(repo: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *argv], shell=False,
        capture_output=True, text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True, text=True)
    _git(repo, "config", "user.email", "runtime@example.test")
    _git(repo, "config", "user.name", "Runtime Test")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _git_broker() -> PermissionBroker:
    return PermissionBroker(PermissionPolicy(allowed={
        PermissionKind.FILE_READ, PermissionKind.GIT_BRANCH,
        PermissionKind.GIT_COMMIT, PermissionKind.GIT_PUSH,
    }))


def test_stable_teammate_session_survives_tasks_messages_and_restart():
    agent = _agent()
    supervisor = get_teammate_supervisor()
    session = supervisor.ensure_session(agent)
    identity = (session.agent_id, session.session_id, session.thread_id,
                session.checkpoint_namespace)

    for task_id in ("first", "second"):
        session.transition(TeammateLifecycle.CLAIMING)
        session.current_task_id = task_id
        session.transition(TeammateLifecycle.RUNNING)
        session.transition(TeammateLifecycle.IDLE)
        session.current_task_id = None
        supervisor.persist(session)

    TeammateCommandQueue(session.session_id).put(
        TeammateCommandType.MESSAGE.value, {"content": "change the next safe step"},
    )
    observed = supervisor.actor_for(agent).safety_point()
    assert observed["messages"][0]["content"] == "change the next safe step"
    assert session.lifecycle_state == TeammateLifecycle.IDLE

    reset_teammate_supervisor()
    restored = get_teammate_supervisor().ensure_session(agent)
    assert (restored.agent_id, restored.session_id, restored.thread_id,
            restored.checkpoint_namespace) == identity
    assert restored.conversation_state["messages"][0]["content"].startswith("change")


def test_sqlite_claim_is_atomic_across_board_instances():
    board = get_task_board()
    board.create_task("hot", "run_claim", "hot", "one winner")
    other = TaskBoard(persist=True)
    other.restore_run("run_claim")
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def claim(candidate: TaskBoard, agent_id: str) -> None:
        barrier.wait()
        results.append(candidate.claim("hot", agent_id, run_id="run_claim").success)

    threads = [threading.Thread(target=claim, args=(board, "a")),
               threading.Thread(target=claim, args=(other, "b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(results) == 1


def test_transactional_graph_mutations_are_idempotent_and_reject_cycles():
    graph = TaskGraph(root_task_id="a")
    graph.add_node(TaskNode(id="a", title="A", objective="A"))
    graph.add_node(TaskNode(id="b", title="B", objective="B", dependencies=["a"]))
    service = TransactionalTaskService()
    service.register_initial_graph("run_graph", graph)
    payload = TaskNode(id="c", title="C", objective="C", dependencies=["b"]).model_dump(mode="json")
    first = service.create_task("run_graph", "lead", payload, mutation_id="same-mutation")
    second = service.create_task("run_graph", "lead", payload, mutation_id="same-mutation")
    assert first.version == second.version
    assert list(second.graph.nodes).count("c") == 1
    with pytest.raises(ValueError):
        service.add_dependency("run_graph", "lead", "a", "c")


def test_permission_cannot_be_self_approved_and_persists_run_grant():
    broker = PermissionBroker()
    with pytest.raises(PermissionRequired) as pending:
        broker.authorize(run_id="run_perm", agent_id="agent_a",
                         kind=PermissionKind.NETWORK, operation="fetch",
                         parameters={"host": "example.test"})
    request = pending.value.request
    with pytest.raises(PermissionError):
        broker.decide(request.request_id, PermissionDecision.APPROVE_FOR_RUN,
                      decided_by="agent:agent_a")
    broker.decide(request.request_id, PermissionDecision.APPROVE_FOR_RUN,
                  decided_by="user:owner", reason="test fixture")
    restarted = PermissionBroker()
    assert restarted.authorize(run_id="run_perm", agent_id="agent_a",
                               kind=PermissionKind.NETWORK, operation="fetch",
                               parameters={"host": "example.test"})


def test_shell_argv_blocks_injection_and_supports_cancellation(tmp_path):
    runner = ShellCommandRunner()
    marker = tmp_path / "injected"
    result = runner.run(["echo", "safe", "&&", "touch", str(marker)], cwd=str(tmp_path))
    assert result.returncode == 0
    assert not marker.exists()

    cancel = threading.Event()
    box: dict[str, object] = {}

    def run_long() -> None:
        box["result"] = runner.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(tmp_path), timeout=60, cancel_token=cancel,
        )

    thread = threading.Thread(target=run_long)
    thread.start()
    time.sleep(0.15)
    cancel.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert box["result"].cancelled is True
    assert box["result"].cancellation_phase == "cancelled_during_tool"


def test_shell_policy_has_explicit_unix_cmd_and_powershell_boundaries():
    policy = ShellPolicyEngine()
    assert policy.classify(["ls", "-la"]) == CommandCategory.READ_ONLY
    assert policy.classify(["cmd.exe", "/c", "dir"]) == CommandCategory.READ_ONLY
    assert policy.classify(["cmd.exe", "/c", "dir & del x"]) == CommandCategory.UNKNOWN
    assert policy.classify(
        ["pwsh", "-Command", "Get-ChildItem ."]
    ) == CommandCategory.READ_ONLY
    assert policy.classify(
        ["pwsh", "-Command", "Get-ChildItem .; Remove-Item x"]
    ) == CommandCategory.UNKNOWN


def test_artifact_dependency_flow_rejects_unverified_missing_and_tampered(tmp_path):
    from app.multiagent.parallel_scheduler import ParallelTeamScheduler
    from app.multiagent.verifier import Verifier

    store = ArtifactStore(str(tmp_path))
    artifact = store.create(run_id="run_art", task_id="upstream", type=ArtifactType.CODE,
                            relative_path="artifacts/upstream/a.py", content="x = 1\n",
                            produced_by="agent_a")
    board = get_task_board()
    board.create_task("upstream", "run_art", "up", "up")
    board.create_task("downstream", "run_art", "down", "down",
                      dependencies=["upstream"])
    assert board.claim("upstream", "agent_a", run_id="run_art").success
    assert board.start("upstream", "agent_a", run_id="run_art")
    assert board.mark_produced("upstream", "agent_a", [artifact.id], run_id="run_art")
    assert board.mark_verifying("upstream", run_id="run_art")
    assert board.mark_verified("upstream", run_id="run_art")
    store.mark_verified(artifact.id)
    scheduler = ParallelTeamScheduler(
        "run_art", verifier=Verifier(
            llm_rubric=LLMRubricVerifier(model_available=False), artifact_store=store,
        ),
    )
    downstream = board.get("downstream", run_id="run_art")
    ids, refs = scheduler._collect_dependency_artifacts(downstream)
    assert ids == [artifact.id]
    assert refs[0]["content_hash"] == artifact.content_hash
    assert refs[0]["verification_state"] == ArtifactStatus.VERIFIED.value

    Path(tmp_path, artifact.path).write_text("tampered\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="artifact_integrity_failed"):
        scheduler._collect_dependency_artifacts(downstream)
    Path(tmp_path, artifact.path).unlink()
    with pytest.raises(RuntimeError, match="artifact_integrity_failed"):
        scheduler._collect_dependency_artifacts(downstream)


def test_nonempty_wrong_artifact_never_passes_without_semantic_verifier():
    result = LLMRubricVerifier(model_available=False).verify(
        "return the correct implementation", {"wrong.py": {"content": "raise RuntimeError()"}},
    )
    assert result.verdict == Verdict.REPAIR
    assert any(f.criterion == "semantic_verifier_unavailable" for f in result.failed_criteria)


def test_worktrees_are_isolated_integrate_commits_and_survive_restart(tmp_path):
    source = _repository(tmp_path)
    repository = RepositoryWorkspaceManager(str(source), str(tmp_path / "run"))
    broker = _git_broker()
    worktrees = AgentWorktreeManager(repository, permission_broker=broker)
    a = worktrees.acquire("run_git", "agent_a")
    b = worktrees.acquire("run_git", "agent_b")
    assert a.worktree_path != b.worktree_path
    assert not (source / "a.txt").exists()
    Path(a.worktree_path, "a.txt").write_text("a\n", encoding="utf-8")
    Path(b.worktree_path, "b.txt").write_text("b\n", encoding="utf-8")

    integration = GitIntegrationManager(repository, permission_broker=broker)
    sha_a = integration.commit(a, "agent a", run_id="run_git", agent_id="agent_a")
    sha_b = integration.commit(b, "agent b", run_id="run_git", agent_id="agent_b")
    assert integration.integrate(MergeQueueItem(
        "qa", "run_git", "agent_a", sha_a, a.branch,
    )).status == "integrated"
    assert integration.integrate(MergeQueueItem(
        "qb", "run_git", "agent_b", sha_b, b.branch,
    )).status == "integrated"
    assert Path(integration.integration_path, "a.txt").is_file()
    assert Path(integration.integration_path, "b.txt").is_file()
    assert not (source / "a.txt").exists()
    assert worktrees.release(a) is False  # unmerged/unpushed commit is retained
    restarted = AgentWorktreeManager(repository, permission_broker=broker)
    assert restarted.get("run_git", "agent_a").worktree_path == a.worktree_path
    with pytest.raises(PermissionError, match="protected branch"):
        integration.push("main", run_id="run_git", agent_id="agent_a")


def test_worktree_environment_copy_is_explicit_gitignored_and_secret_safe(tmp_path):
    source = _repository(tmp_path)
    (source / ".gitignore").write_text(".runtime.env\nprivate.pem\n", encoding="utf-8")
    _git(source, "add", ".gitignore")
    _git(source, "commit", "-m", "ignore local runtime files")
    (source / ".runtime.env").write_text("MODE=test\n", encoding="utf-8")
    (source / "private.pem").write_text("secret\n", encoding="utf-8")
    repository = RepositoryWorkspaceManager(str(source), str(tmp_path / "run-env"))
    broker = _git_broker()

    default_lease = AgentWorktreeManager(
        repository, permission_broker=broker,
    ).acquire("run_env", "default")
    assert not Path(default_lease.worktree_path, ".runtime.env").exists()

    allowed_lease = AgentWorktreeManager(
        repository, permission_broker=broker,
        environment_file_allowlist=[".runtime.env"],
    ).acquire("run_env", "allowed")
    assert Path(allowed_lease.worktree_path, ".runtime.env").read_text() == "MODE=test\n"

    with pytest.raises(PermissionError, match="credential"):
        AgentWorktreeManager(
            repository, permission_broker=broker,
            environment_file_allowlist=["private.pem"],
        ).acquire("run_env", "secret")


def test_same_file_worktree_changes_are_reported_as_conflict(tmp_path):
    source = _repository(tmp_path)
    repository = RepositoryWorkspaceManager(str(source), str(tmp_path / "run-conflict"))
    broker = _git_broker()
    worktrees = AgentWorktreeManager(repository, permission_broker=broker)
    a = worktrees.acquire("run_conflict", "agent_a")
    b = worktrees.acquire("run_conflict", "agent_b")
    Path(a.worktree_path, "shared.txt").write_text("from a\n", encoding="utf-8")
    Path(b.worktree_path, "shared.txt").write_text("from b\n", encoding="utf-8")
    integration = GitIntegrationManager(repository, permission_broker=broker)
    sha_a = integration.commit(a, "agent a", run_id="run_conflict", agent_id="agent_a")
    sha_b = integration.commit(b, "agent b", run_id="run_conflict", agent_id="agent_b")
    assert integration.integrate(MergeQueueItem(
        "ca", "run_conflict", "agent_a", sha_a, a.branch,
    )).status == "integrated"
    conflict = integration.integrate(MergeQueueItem(
        "cb", "run_conflict", "agent_b", sha_b, b.branch,
    ))
    assert conflict.status == "conflict"
    assert conflict.conflicts == ["shared.txt"]


def test_dynamic_team_enforces_budget_depth_and_parent_tool_subset():
    profiles = get_capability_registry()
    parent_profile = profiles.get_profile("coder")
    parent = get_agent_registry().create_agent(
        profile_id=parent_profile.id, name="parent", role="coder", team_id="team",
        run_id="run_dynamic", capabilities=sorted(parent_profile.capabilities),
    )
    manager = DynamicTeamManager(
        budget=TeamBudget(max_team_size=2, max_agents_per_run=2, max_spawn_depth=1),
    )
    child = manager.spawn(
        run_id="run_dynamic", team_id="team", required_capabilities={"testing"},
        requested_by=parent.agent_id, parent_agent_id=parent.agent_id,
    )
    child_profile = profiles.get_profile(child.profile_id)
    assert set(child_profile.tool_policy.allowed_tools).issubset(
        parent_profile.tool_policy.allowed_tools
    )
    with pytest.raises(RuntimeError, match="max_team_size"):
        manager.spawn(run_id="run_dynamic", team_id="team",
                      required_capabilities={"testing"}, requested_by=parent.agent_id,
                      parent_agent_id=child.agent_id)


def test_missing_capability_never_falls_back_to_default_coder():
    assert get_capability_registry().select_profile({"nonexistent-superpower"}) is None


def test_lifecycle_hook_can_block_completion_and_command_hook_is_structured(tmp_path):
    engine = get_lifecycle_hook_engine()
    engine.register(
        LifecycleEvent.TASK_COMPLETED,
        lambda context: HookResult(block=True, feedback="review still required"),
        scope="project",
    )
    result = engine.emit(LifecycleEvent.TASK_COMPLETED, {
        "run_id": "run_hook", "agent_id": "a", "task_id": "t",
    })
    assert result.block
    assert "review" in result.feedback
    hook_id = engine.register_command_hook(
        LifecycleEvent.BEFORE_TOOL_USE,
        [sys.executable, "-c", "print('hook ok')"], cwd=str(tmp_path),
    )
    assert hook_id.startswith("hook_")
    assert engine.emit(LifecycleEvent.BEFORE_TOOL_USE, {
        "run_id": "run_hook", "agent_id": "a", "task_id": "t",
    }).allow


def test_event_envelopes_are_monotonic_replayable_and_structured():
    history = get_agent_run_history()
    for index in range(3):
        history.record_event(
            event_id=make_run_event_id(), run_id="run_events",
            event_type="Progress", payload={"index": index},
        )
    all_events = history.list_event_envelopes("run_events")
    assert [event["sequence"] for event in all_events] == [1, 2, 3]
    assert all_events[0]["payload"] == {"index": 0}
    assert [event["sequence"] for event in
            history.list_event_envelopes("run_events", after_sequence=1)] == [2, 3]


def test_tool_side_effect_recovery_never_replays_ambiguous_write():
    journal = ToolSideEffectJournal()
    invocation = ToolInvocation(
        idempotency_key="stable-key", run_id="run_tool", agent_id="a", task_id="t",
        tool_name="create_file", arguments={"path": "x"}, side_effecting=True,
    )
    _, created = journal.begin(invocation)
    assert created
    _, duplicate_created = journal.begin(invocation.model_copy())
    assert not duplicate_created
    recovered = journal.recover_incomplete("run_tool")
    assert recovered[0].status == ToolInvocationStatus.NEEDS_HUMAN


def test_high_risk_plan_waits_for_persistent_user_decision():
    service = PlanApprovalService()
    plan = service.submit(TeammatePlan(
        run_id="run_plan", agent_id="agent_a", task_id="task_a",
        files=["infra/prod.tf"], steps=["change production infrastructure"],
        test_strategy=["terraform validate"], risks=["production impact"],
        rollback="revert commit",
    ))
    assert plan.status == PlanStatus.WAITING_PLAN_APPROVAL
    with pytest.raises(PermissionError):
        service.decide(plan.plan_id, True, decided_by="agent:agent_a")
    decided = PlanApprovalService().decide(
        plan.plan_id, True, decided_by="user:owner", feedback="approved for this run",
    )
    assert decided.status == PlanStatus.PLAN_APPROVED
    assert PlanApprovalService().get(plan.plan_id).decided_by == "user:owner"
