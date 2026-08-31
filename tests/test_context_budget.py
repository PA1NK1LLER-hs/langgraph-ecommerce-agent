"""上下文 token 预算模块测试（借鉴 Codex context_window / compact）。"""

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from agent.context_budget import (
    SUMMARY_PREFIX,
    estimate_tokens,
    has_summary_prefix,
    is_noise_message,
    strip_images,
    messages_tokens,
    truncate_messages_to_budget,
)


class TestEstimateTokens:
    def test_empty_text_zero(self):
        assert estimate_tokens("") == 0

    def test_nonempty_positive(self):
        assert estimate_tokens("hello world this is a test") > 0

    def test_ascii_estimate_positive(self):
        # tiktoken 可用时走 tiktoken（重复单字符可能被合并为 1 token），
        # 只断言返回正数而非依赖具体编码策略。
        assert estimate_tokens("a" * 100) > 0


class TestSummaryPrefix:
    def test_has_summary_prefix_detects(self):
        msg = SystemMessage(content=f"{SUMMARY_PREFIX}\n这是摘要内容")
        assert has_summary_prefix(msg) is True

    def test_no_prefix(self):
        msg = HumanMessage(content="普通消息")
        assert has_summary_prefix(msg) is False

    def test_list_content_no_prefix(self):
        msg = HumanMessage(content=[{"type": "text", "text": "你好"}])
        assert has_summary_prefix(msg) is False


class TestNoise:
    def test_pure_image_is_noise(self):
        msg = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}])
        assert is_noise_message(msg) is True

    def test_text_is_not_noise(self):
        msg = HumanMessage(content="今天天气怎么样")
        assert is_noise_message(msg) is False

    def test_env_prefix_is_noise(self):
        msg = HumanMessage(content="Environment: HOME=/root")
        assert is_noise_message(msg) is True

    def test_empty_text_is_noise(self):
        msg = HumanMessage(content="   ")
        assert is_noise_message(msg) is True


class TestStripImages:
    def test_removes_image_keeps_text(self):
        content = [
            {"type": "text", "text": "请识别"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ]
        out = strip_images(content)
        assert len(out) == 1
        assert out[0]["type"] == "text"

    def test_str_passthrough(self):
        assert strip_images("hello") == "hello"


class TestMessagesTokens:
    def test_skips_summary_and_noise(self):
        msgs = [
            SystemMessage(content=f"{SUMMARY_PREFIX}摘要"),
            HumanMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}]),
            HumanMessage(content="正常内容正常内容"),
        ]
        total = messages_tokens(msgs)
        only_text = estimate_tokens("正常内容正常内容")
        assert total == only_text


class TestTruncateToBudget:
    def test_keeps_most_recent(self):
        long_text = "词" * 500  # 一条消息约几百 token
        msgs = [HumanMessage(content=f"old-{i} {long_text}") for i in range(5)]
        # 预算只够约 1 条消息
        kept, dropped = truncate_messages_to_budget(msgs, budget_tokens=estimate_tokens("词" * 500) + 10)
        assert dropped >= 4
        assert len(kept) >= 1
        # 保留的是最后一条
        assert kept[-1].content.startswith("old-4")

    def test_no_truncation_when_under_budget(self):
        msgs = [HumanMessage(content="短消息")]
        kept, dropped = truncate_messages_to_budget(msgs, budget_tokens=10000)
        assert dropped == 0
        assert len(kept) == 1
