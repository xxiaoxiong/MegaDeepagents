"""Phase G 收尾：Mailbox 持久化 + Checkpoint loader + Orchestrator telemetry。"""
from __future__ import annotations

import pytest

from app.core.config import settings as cfg


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    from app.multiagent.store import close_connection
    close_connection()
    cfg.sqlite_path = str(tmp_path / "test_phase_g_polish.sqlite3")
    yield
    close_connection()


class TestMailboxPersistence:
    def test_flush_and_restore_roundtrip(self):
        from app.multiagent.mailbox import (
            Mailbox, MailboxMessage, MessageSeverity,
        )
        from app.multiagent.phase_g_store import get_agent_run_history

        m = Mailbox()
        m.send(MailboxMessage(
            message_id="m1",
            from_agent_id="planner", to_agent_id="coder",
            run_id="rA",
            title="do stuff", content="please code X",
            severity=MessageSeverity.INFO,
        ))
        m.send(MailboxMessage(
            message_id="m2",
            from_agent_id="planner", to_agent_id="reviewer",
            run_id="rA",
            title="review this", content="check Y",
            severity=MessageSeverity.INFO,
        ))
        n = m.flush_to_db("rA")
        assert n == 2

        h = get_agent_run_history()
        rows = h.list_mailbox_messages(run_id="rA")
        assert len(rows) == 2

        # 恢复到新 mailbox
        m2 = Mailbox()
        restored = m2.restore_from_db("rA")
        assert restored == 2

        msgs_to_coder = m2.receive("coder")
        assert len(msgs_to_coder) == 1
        assert msgs_to_coder[0].content == "please code X"

    def test_consumed_messages_not_re_delivered_on_restore(self):
        """已 consumed 的消息恢复时不重新塞回 inbox。"""
        from app.multiagent.mailbox import (
            Mailbox, MailboxMessage, MessageSeverity,
        )
        from app.multiagent.phase_g_store import get_agent_run_history

        m = Mailbox()
        m.send(MailboxMessage(
            message_id="m_old",
            from_agent_id="planner", to_agent_id="coder",
            run_id="rB",
            title="consumed",
            content="already done",
            severity=MessageSeverity.INFO,
        ))
        m.receive("coder")  # 消费掉
        m.flush_to_db("rB")

        # 模拟已 consumed 标记
        h = get_agent_run_history()
        h.mark_mailbox_consumed("m_old")

        m2 = Mailbox()
        m2.restore_from_db("rB")
        # 不应重新出现在 inbox
        assert len(m2._inboxes.get("coder", [])) == 0


class TestCheckpointLoader:
    def test_loader_returns_none_for_nonexistent_agent_id(self):
        """没有 agent_instances 记录时返回 None（不抛异常）。"""
        from app.multiagent.resume_coordinator import _load_checkpoint_sync
        ckpt = _load_checkpoint_sync(
            "any_thread", "no_such_agent_id", "any_run"
        )
        assert ckpt is None

    def test_loader_returns_none_for_no_thread_id(self):
        """agent_instances 有记录但没有 thread_id 时返回 None。"""
        from app.multiagent.phase_g_store import get_agent_run_history
        from app.multiagent.resume_coordinator import _load_checkpoint_sync

        h = get_agent_run_history()
        h.upsert_agent_instance(
            agent_id="agent_a", team_id="t", run_id="r",
            profile_id="p", name="A", role="coder",
            session_id="s", thread_id="",  # 显式空
            checkpoint_namespace="ns", status="idle",
        )
        ckpt = _load_checkpoint_sync("any_thread", "agent_a", "r")
        assert ckpt is None


class TestOrchestratorTelemetry:
    def test_emit_event_writes_to_db(self):
        """验证 _emit_event 写入 team_events 表。"""
        from app.multiagent.orchestrator import SimpleOrchestrator
        from app.multiagent.team_run_context import TeamRunContext, TeamRunMode
        from app.multiagent.phase_g_store import get_agent_run_history

        ctx = TeamRunContext(
            goal="test", team_id="t1", run_id="run_telemetry_y",
            mode=TeamRunMode.TASK_TEAM, checkpoint_namespace="ns",
            workspace_root="/tmp/test_telemetry",
        )
        orch = SimpleOrchestrator()
        orch._ctx = ctx
        orch._emit_event("manual_test", {"msg": "hello from test"})

        h = get_agent_run_history()
        events = h.list_events("run_telemetry_y", event_type="orchestrator:manual_test")
        assert len(events) == 1, f"期望 1 个事件，实际 {len(events)}"
        payload = events[0].get("payload", {})
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        assert payload.get("msg") == "hello from test"
