"""多 Agent 消息协议 + mailbox 测试（借鉴 Codex multi-agent）。"""

import pytest

from agent.specialists import (
    AgentMessage,
    Mailbox,
    encode_message,
    decode_message,
    NEW_TASK,
    MESSAGE,
    FINAL_ANSWER,
)


class TestAgentMessage:
    def test_valid_kinds(self):
        for kind in (NEW_TASK, MESSAGE, FINAL_ANSWER):
            AgentMessage(kind=kind, task="t1", sender="researcher", payload="hello")

    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError):
            AgentMessage(kind="bogus", task="t1")

    def test_roundtrip_dict(self):
        m = AgentMessage(kind=FINAL_ANSWER, task="t1", sender="coder", payload="答案")
        d = m.to_dict()
        assert decode_message(d) == m

    def test_encode_decode(self):
        d = encode_message(NEW_TASK, "task-a", sender="supervisor", payload="去搜索")
        m = decode_message(d)
        assert m.kind == NEW_TASK
        assert m.task == "task-a"
        assert m.sender == "supervisor"


class TestMailbox:
    def test_collect_by_task(self):
        mb = Mailbox()
        mb.send(AgentMessage(kind=NEW_TASK, task="a", payload="任务"))
        mb.send(AgentMessage(kind=MESSAGE, task="a", sender="researcher", payload="进度"))
        mb.send(AgentMessage(kind=FINAL_ANSWER, task="a", sender="researcher", payload="结果"))
        mb.send(AgentMessage(kind=FINAL_ANSWER, task="b", sender="coder", payload="另一个"))

        assert len(mb.collect("a")) == 3
        assert len(mb.final_answers("a")) == 1
        assert mb.final_answers("a")[0].payload == "结果"
        assert mb.all_tasks() == ["a", "b"]

    def test_empty_task(self):
        mb = Mailbox()
        assert mb.collect("nonexistent") == []
        assert mb.final_answers("nonexistent") == []
