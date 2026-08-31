"""对话摘要器 — 长对话历史自动压缩 + 滑动窗口管理。

借鉴 Codex harness 的上下文管理（``compact.rs`` / ``context_window.rs``）：

- **触发**：消息数超阈值 **或** 新增 token 超预算时触发（原来是只看消息数）。
- **去重**：以 ``SUMMARY_PREFIX`` 开头的消息视为已摘要，收集「新消息」时跳过，
  避免把已摘要内容反复喂回摘要模型（对应 Codex ``SUMMARY_PREFIX`` 去重语义）。
- **清洗**：摘要前剥离图片块、跳过环境信息/安全提示等噪声 user 消息，
  防止 base64 图片与噪声污染摘要模型的上下文。
- **预算**：保留最近消息按 token 预算（而非固定条数），超限丢弃最老一条。

原 API 契约（``should_summarize(messages)`` / ``summarize(messages, existing_summary)``）
保持不变，新增参数均为可选，向后兼容。
"""

from __future__ import annotations

import logging

from .context_budget import (
    SUMMARY_PREFIX,
    DEFAULT_KEEP_TOKENS,
    DEFAULT_TRIGGER_TOKENS,
    messages_tokens,
    truncate_messages_to_budget,
    strip_images,
    is_noise_message,
    has_summary_prefix,
)

logger = logging.getLogger(__name__)

# 触发摘要的消息数阈值（兼容旧行为）
TRIGGER_THRESHOLD = 20
# 保留最近 N 条完整消息（兼容旧行为，token 预算未启用时的兜底）
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

    def __init__(
        self,
        trigger_threshold: int = TRIGGER_THRESHOLD,
        keep_recent: int = KEEP_RECENT,
        trigger_tokens: int = DEFAULT_TRIGGER_TOKENS,
        keep_tokens: int = DEFAULT_KEEP_TOKENS,
    ):
        self.trigger_threshold = trigger_threshold
        self.keep_recent = keep_recent
        self.trigger_tokens = trigger_tokens
        self.keep_tokens = keep_tokens

    def should_summarize(self, messages: list, growth_tokens: int | None = None) -> bool:
        """判断是否需要压缩消息历史。

        触发条件（任一满足即可）：
        - 消息数达到阈值（兼容旧行为）；
        - 新增对话 token 数超过预算（Codex 风格，避免单条超长消息撑爆上下文）。
        """
        if len(messages) >= self.trigger_threshold:
            return True
        if growth_tokens is not None and growth_tokens >= self.trigger_tokens:
            return True
        return False

    def _collect_early(self, messages: list) -> tuple[list, list[str]]:
        """切分「早期消息」与「最近消息」，并收集早期消息的纯文本行。

        早期消息中的图片块被剥离、噪声消息被跳过、已摘要消息被跳过。

        Returns:
            (early_lines, 保留的最近消息列表)。
        """
        recent = messages[-self.keep_recent:] if len(messages) > self.keep_recent else messages
        early = messages[:-self.keep_recent] if len(messages) > self.keep_recent else []

        lines: list[str] = []
        for m in early:
            # 已摘要消息不再二次摘要
            if has_summary_prefix(m):
                continue
            if is_noise_message(m):
                continue
            role = getattr(m, "type", "unknown")
            content = strip_images(getattr(m, "content", ""))
            if isinstance(content, list):
                content = " ".join(
                    str(c.get("text", "")) for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            content = str(content)[:300]  # 截断
            if content.strip():
                lines.append(f"[{role}]: {content.strip()}")

        return lines, recent

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

        lines, _ = self._collect_early(messages)
        if not lines:
            return existing_summary or ""

        conversation_text = "\n".join(lines)

        if existing_summary:
            prompt_text = f"## 已有摘要\n{existing_summary}\n\n## 新增对话\n{conversation_text}"
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
                logger.info("对话摘要已生成: %d chars (from %d messages)", len(summary), len(lines))
                return summary
        except Exception:
            logger.warning("摘要生成失败", exc_info=True)

        return existing_summary or ""

    def compact_messages(
        self,
        messages: list,
        summary: str,
    ) -> list:
        """构建压缩后的消息列表：摘要 + 最近消息（按 token 预算保留）。

        与旧实现兼容：保留最近消息的条数默认用 ``keep_recent``；
        仅当 ``keep_tokens > 0`` 时按 token 预算截断（Codex 风格滑动窗口）。

        Args:
            messages: 原始消息列表。
            summary: 摘要文本。

        Returns:
            压缩后的消息列表。
        """
        from langchain_core.messages import SystemMessage

        recent = messages[-self.keep_recent:] if len(messages) > self.keep_recent else messages

        # token 预算截断（保留最近，丢弃最老）
        if self.keep_tokens > 0:
            recent, _dropped = truncate_messages_to_budget(recent, self.keep_tokens)

        compact: list = []

        # 摘要作为 system message 注入
        if summary:
            compact.append(SystemMessage(
                content=f"{SUMMARY_PREFIX}\n{summary}\n\n请结合以上摘要理解用户的后续问题。"
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


def estimate_growth_tokens(messages: list, summary: str = "") -> int:
    """估算「新增对话」（不含已有摘要与噪声）的 token 数。

    供 ``should_summarize`` 的 token 触发条件使用；summary 用于跳过已摘要消息。
    """
    return messages_tokens(messages, skip_summary=True, skip_noise=True)
