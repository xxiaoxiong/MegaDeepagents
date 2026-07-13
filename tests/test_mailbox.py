"""Mailbox 与治理测试。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.multiagent.mailbox import (
    Mailbox,
    MailboxMessage,
    MessageSeverity,
    PolicyViolation,
    make_message_id,
    make_thread_id,
    get_mailbox,
    reset_mailbox,
)


@pytest.fixture(autouse=True)
def _reset_mailbox_each():
    reset_mailbox()
    yield
    reset_mailbox()


def _new_msg(
    sender="agent_a",
    to="agent_b",
    run_id="r1",
    title="t",
    content="c",
    severity=MessageSeverity.INFO,
) -> MailboxMessage:
    return MailboxMessage(
        message_id=make_message_id(),
        from_agent_id=sender,
        to_agent_id=to,
        run_id=run_id,
        title=title,
        content=content,
        severity=severity,
    )


class TestMailboxBasics:
    def test_send_and_receive(self):
        m = Mailbox()
        msg = _new_msg()
        assert m.send(msg)
        out = m.receive("agent_b")
        assert len(out) == 1
        assert out[0].message_id == msg.message_id
        assert out[0].consumed_at is not None

    def test_peek_does_not_consume(self):
        m = Mailbox()
        m.send(_new_msg())
        assert len(m.peek("agent_b")) == 1
        assert len(m.peek("agent_b")) == 1  # 还是 1 条
        out = m.receive("agent_b")
        assert len(out) == 1

    def test_inbox_size(self):
        m = Mailbox()
        m.send(_new_msg())
        m.send(_new_msg())
        assert m.inbox_size("agent_b") == 2


class TestMailboxGovernance:
    def test_blocklist(self):
        m = Mailbox()
        m.block("agent_a")
        assert not m.send(_new_msg())  # 被拦截
        assert m.inbox_size("agent_b") == 0
        # 解封
        m.unblock("agent_a")
        assert m.send(_new_msg())
        assert m.inbox_size("agent_b") == 1

    def test_policy_hook_block_send(self):
        m = Mailbox()

        def policy(msg: MailboxMessage) -> None:
            if "block_me" in msg.content:
                raise PolicyViolation("forbidden")

        m.add_policy_hook(policy)
        assert not m.send(_new_msg(content="block_me please"))
        assert m.send(_new_msg(content="hello"))
        assert m.inbox_size("agent_b") == 1

    def test_rate_limit(self):
        m = Mailbox()
        m._rate_limit_per_minute = 3
        # 发 3 条 OK，第 4 条限流
        for _ in range(3):
            assert m.send(_new_msg())
        assert not m.send(_new_msg())

    def test_capacity_overflow_drops_oldest(self):
        m = Mailbox(per_agent_max_size=2)
        m.send(_new_msg(content="m1"))
        m.send(_new_msg(content="m2"))
        m.send(_new_msg(content="m3"))
        # 容量 2，丢最老的；现在 inbox 应有 m2, m3
        out = m.receive("agent_b")
        contents = [x.content for x in out]
        assert "m1" not in contents
        assert "m2" in contents and "m3" in contents


class TestMailboxBroadcast:
    def test_broadcast_run(self):
        m = Mailbox()
        msg = MailboxMessage(
            message_id=make_message_id(),
            from_agent_id="agent_a", from_role="pm",
            to_agent_id=None,  # broadcast
            run_id="r1",
            title="bcast", content="hi all",
        )
        n = m.broadcast_run("r1", ["agent_b", "agent_c", "agent_a"], msg)
        # agent_a 是发送者跳过，agent_b / agent_c 各 1 条
        assert n == 2
        assert m.inbox_size("agent_b") == 1
        assert m.inbox_size("agent_c") == 1
        assert m.inbox_size("agent_a") == 0

    def test_broadcast_role(self):
        m = Mailbox()
        msg = MailboxMessage(
            message_id=make_message_id(),
            from_agent_id="pm1", from_role="pm",
            to_role="coder",
            run_id="r1",
            title="bcast", content="hi coders",
        )
        targets = [("coder1", "coder"), ("coder2", "coder"), ("tester1", "tester"), ("pm1", "pm")]
        n = m.broadcast_role("r1", "coder", targets, msg)
        # coder1 + coder2 收到，pm1 是发送者 skip，tester1 不匹配
        assert n == 2
        assert m.inbox_size("coder1") == 1
        assert m.inbox_size("coder2") == 1
        assert m.inbox_size("tester1") == 0


class TestMailboxThread:
    def test_reply_chain(self):
        m = Mailbox()
        tid = make_thread_id()
        m1 = MailboxMessage(
            message_id=make_message_id(),
            from_agent_id="agent_a", to_agent_id="agent_b",
            run_id="r1", title="start", content="hello", thread_id=tid,
        )
        m.send(m1)
        m2 = MailboxMessage(
            message_id=make_message_id(),
            from_agent_id="agent_b", to_agent_id="agent_a",
            run_id="r1", title="reply", content="ok", thread_id=tid, reply_to=m1.message_id,
        )
        m.send(m2)
        msgs_in_run = m.list_messages_in_run("r1")
        assert len(msgs_in_run) == 2
        replies = [x for x in msgs_in_run if x.reply_to == m1.message_id]
        assert len(replies) == 1 and replies[0].thread_id == tid


class TestMailboxSnapshotRestore:
    def test_snapshot_and_restore(self):
        m = Mailbox()
        m.send(_new_msg())
        m.send(_new_msg(content="more"))
        snap = m.snapshot()
        # 重建
        m2 = Mailbox()
        m2.restore(snap)
        assert m2.inbox_size("agent_b") == 2
        msgs = m2.list_messages_in_run("r1")
        assert len(msgs) == 2

    def test_blocklist_survives_snapshot(self):
        m = Mailbox()
        m.block("agent_x")
        snap = m.snapshot()
        m2 = Mailbox()
        m2.restore(snap)
        assert m2.is_blocked("agent_x")
