"""对话摘要器测试（含 token 预算触发 / 摘要去重 / 图片剥离）。"""

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from agent.summarizer import (
    ConversationSummarizer,
    SUMMARY_PREFIX,
    get_summarizer,
    estimate_growth_tokens,
)


def _msgs(n: int) -> list:
    out = []
    for i in range(n // 2 + 1):
        out.append(HumanMessage(content=f"消息{i}"))
        out.append(AIMessage(content=f"回复{i}"))
    return out


class TestShouldSummarize:
    def test_message_count_trigger(self):
        s = ConversationSummarizer(trigger_threshold=20)
        assert s.should_summarize(_msgs(20)) is True
        assert s.should_summarize(_msgs(5)) is False

    def test_token_trigger(self):
        s = ConversationSummarizer(trigger_threshold=999, trigger_tokens=100)
        # 消息数不达标，但新增 token 超预算 → 触发
        assert s.should_summarize(_msgs(2), growth_tokens=500) is True
        assert s.should_summarize(_msgs(2), growth_tokens=10) is False


class TestCollectEarly:
    def test_skips_existing_summary(self):
        s = ConversationSummarizer(trigger_threshold=20, keep_recent=2)
        msgs = [
            SystemMessage(content=f"{SUMMARY_PREFIX}旧摘要"),
            HumanMessage(content="真实消息1"),
            AIMessage(content="回复1"),
            HumanMessage(content="真实消息2"),
            AIMessage(content="回复2"),
        ]
        lines, recent = s._collect_early(msgs)
        # 摘要消息被跳过，不应出现在待压缩的 early 行里
        assert not any("旧摘要" in ln for ln in lines)
        # recent 保留最近 2 条
        assert len(recent) == 2

    def test_strips_image_blocks(self):
        s = ConversationSummarizer(trigger_threshold=20, keep_recent=1)
        msgs = [
            HumanMessage(content=[
                {"type": "text", "text": "请识别这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxxx"}},
            ]),
            AIMessage(content="好的"),
        ]
        lines, _ = s._collect_early(msgs)
        assert lines
        assert "base64" not in lines[0]  # 图片 base64 不应进入摘要输入
        assert "请识别这张图" in lines[0]


class TestCompactMessages:
    def test_summary_injected_with_prefix(self):
        s = ConversationSummarizer()
        msgs = [HumanMessage(content="你好"), AIMessage(content="你好！")]
        compact = s.compact_messages(msgs, "摘要内容")
        assert compact[0].content.startswith(SUMMARY_PREFIX)
        assert "摘要内容" in compact[0].content

    def test_token_budget_truncates(self):
        s = ConversationSummarizer(keep_tokens=50)
        long_msgs = [HumanMessage(content="很长的内容" * 50)] * 5
        compact = s.compact_messages(long_msgs, "摘要")
        # 摘要占第一条，最近消息被截断到预算内
        assert len(compact) < 6


class TestModule:
    def test_get_summarizer_singleton(self):
        assert get_summarizer() is get_summarizer()

    def test_estimate_growth_tokens_positive(self):
        msgs = [HumanMessage(content="这是一条有意义的用户消息")]
        assert estimate_growth_tokens(msgs) > 0
