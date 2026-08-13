"""对话摘要器 — 长对话历史自动压缩 + 滑动窗口管理。

当消息数超过阈值（默认 20 条）时，自动将早期消息压缩为结构化摘要，
保留最近 N 条完整消息（默认 10 条），在维持回答质量的同时降低 token 消耗。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 触发摘要的消息数阈值
TRIGGER_THRESHOLD = 20
# 保留最近 N 条完整消息
KEEP_RECENT = 10

_SUMMARIZE_SYSTEM = """你是一个对话摘要助手。请将以下对话历史压缩为结构化摘要。

## 摘要格式
提取以下信息并用简洁的中文总结：
1. **关键决策**: 用户做出了哪些重要决定
2. **已完成任务**: 已经完成了哪些工作
3. **用户偏好**: 用户表达了哪些偏好或习惯
4. **当前上下文**: 当前的对话主题和进展情况
5. **待处理事项**: 还有哪些未完成的任务

## 要求
- 只用事实，不编造
- 保持简洁，每条信息不超过一句话
- 如果某类信息不存在，写"无"
"""


class ConversationSummarizer:
    """对话摘要器 — 使用 Flash LLM 将长对话压缩为结构化摘要。

    摘要保留关键信息（决策、任务、偏好），丢弃冗余细节，
    在后续对话中作为上下文注入，避免上下文窗口溢出。
    """

    TRIGGER_THRESHOLD: int = TRIGGER_THRESHOLD
    KEEP_RECENT: int = KEEP_RECENT

    def __init__(self, trigger_threshold: int = TRIGGER_THRESHOLD, keep_recent: int = KEEP_RECENT):
        self.trigger_threshold = trigger_threshold
        self.keep_recent = keep_recent

    def should_summarize(self, messages: list) -> bool:
        """判断是否需要压缩消息历史。"""
        return len(messages) >= self.trigger_threshold

    async def summarize(self, messages: list, existing_summary: str = "") -> str:
        """将消息列表压缩为结构化摘要。

        将早期消息压缩，保留最近消息的完整内容。
        如果已有摘要，新摘要在其基础上更新。

        Args:
            messages: 完整的消息列表。
            existing_summary: 已有的摘要文本（增量更新时使用）。

        Returns:
            压缩后的摘要字符串。
        """
        if not messages:
            return existing_summary or ""

        # 早期消息 → 压缩为摘要；最近消息 → 完整保留
        early = messages[:-self.keep_recent] if len(messages) > self.keep_recent else []
        if not early:
            return existing_summary or ""

        # 格式化早期消息为文本
        lines = []
        for m in early:
            role = getattr(m, "type", "unknown")
            content = getattr(m, "content", "")
            if isinstance(content, list):
                content = " ".join(str(c) for c in content)
            content = str(content)[:300]  # 截断
            if content.strip():
                lines.append(f"[{role}]: {content.strip()}")

        conversation_text = "\n".join(lines)

        context_prefix = ""
        if existing_summary:
            context_prefix = f"## 已有摘要\n{existing_summary}\n\n## 新增对话\n{conversation_text}"
            prompt_text = context_prefix
        else:
            prompt_text = conversation_text

        try:
            from config import LLM_FLASH_MODEL
            from .client_factory import get_async_openai_client

            client = get_async_openai_client()
            resp = await client.chat.completions.create(
                model=LLM_FLASH_MODEL,
                messages=[
                    {"role": "system", "content": _SUMMARIZE_SYSTEM},
                    {"role": "user", "content": prompt_text[:6000]},  # 限制输入长度
                ],
                temperature=0.1,
                max_tokens=500,
            )
            summary = (resp.choices[0].message.content or "").strip()
            if summary:
                logger.info("对话摘要已生成: %d chars (from %d messages)", len(summary), len(early))
                return summary
        except Exception:
            logger.warning("摘要生成失败", exc_info=True)

        return existing_summary or ""

    def compact_messages(
        self,
        messages: list,
        summary: str,
    ) -> list:
        """构建压缩后的消息列表：摘要 + 最近消息。

        Args:
            messages: 原始消息列表。
            summary: 摘要文本。

        Returns:
            压缩后的消息列表。
        """
        from langchain_core.messages import SystemMessage

        recent = messages[-self.keep_recent:] if len(messages) > self.keep_recent else messages
        compact = []

        # 摘要作为 system message 注入
        if summary:
            compact.append(SystemMessage(
                content=f"## 历史对话摘要\n{summary}\n\n请结合以上摘要理解用户的后续问题。"
            ))

        compact.extend(recent)
        return compact


# ---------------------------------------------------------------------------
# 模块单例
# ---------------------------------------------------------------------------

_summarizer: ConversationSummarizer | None = None


def get_summarizer() -> ConversationSummarizer:
    global _summarizer
    if _summarizer is None:
        _summarizer = ConversationSummarizer()
    return _summarizer
