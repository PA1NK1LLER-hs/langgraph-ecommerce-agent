"""上下文 token 预算 — 借鉴 Codex harness 的上下文管理。

Codex 用 ``session/context_window.rs`` 的 token 预算 + ``compact.rs`` 的
``SUMMARY_PREFIX`` 去重来管理上下文。本模块提供纯函数实现，供
``summarizer.py``（压缩触发 / 摘要去重 / 噪声剥离）和
``tool_get_context_remaining``（元认知工具）复用。

所有函数为纯函数，无副作用，便于单元测试。
"""

from __future__ import annotations

# 摘要消息的固定前缀 —— 压缩产物在消息历史里以该前缀开头。
# 收集「待摘要的新消息」时跳过这些消息，避免把已摘要内容再摘要一次
# （对应 Codex ``SUMMARY_PREFIX`` 的去重语义）。
SUMMARY_PREFIX = "## 历史对话摘要"

# 默认上下文窗口大小（DeepSeek 上下文上限）。tool_get_context_remaining
# 用「窗口大小 - 已用 prompt token」估算剩余预算。
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000

# 保留最近消息的默认 token 预算（压缩后保留的滑动窗口大小）。
DEFAULT_KEEP_TOKENS = 20_000

# 触发压缩的默认新增 token 阈值。
DEFAULT_TRIGGER_TOKENS = 24_000

# 明显是噪声/非对话内容的 user 消息前缀（环境信息、安全提示等），
# 压缩和 token 统计时跳过。
_NOISE_PREFIXES = (
    "environment:", "environment：", "环境信息", "安全提示",
    "current time", "当前时间", "system:", "system：",
)


def estimate_tokens(text: str) -> int:
    """估算文本 token 数。

    优先用 tiktoken（cl100k_base，OpenAI 系 tokenizer 的通用近似）；
    tiktoken 不可用时退化为 ``len(text) // 4``（中英文混合的粗略估计）。
    """
    if not text:
        return 0
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _content_text(content) -> str:
    """把消息 content（str / list）转成纯文本，用于 token 估算。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(p for p in parts if p)
    return str(content)


def has_summary_prefix(message) -> bool:
    """消息是否是一条已生成的摘要（以 SUMMARY_PREFIX 开头）。"""
    text = _content_text(getattr(message, "content", ""))
    return text.startswith(SUMMARY_PREFIX)


def is_noise_message(message) -> bool:
    """消息是否为噪声（环境信息 / 安全提示 / 纯图片无文本）。"""
    role = getattr(message, "type", "")
    content = getattr(message, "content", None)
    # 图片块无文本 → 对文本模型无信息量，压缩时跳过
    if isinstance(content, list):
        has_text = any(
            isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            for b in content
        )
        if not has_text:
            return True
    text = _content_text(content).strip().lower()
    if not text:
        return True
    # 工具消息 / 系统消息不计入「对话内容」统计（由调用方决定是否传入）
    if role == "system":
        return False
    return any(text.startswith(p.lower()) for p in _NOISE_PREFIXES)


def strip_images(content):
    """从多模态 content 中剥离图片块，只保留文本块。

    返回与输入同结构（str 原样返回；list 返回只含 text 块的新 list）。
    用于压缩 / 摘要前的清洗，避免 base64 图片进入文本模型的上下文。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        kept: list = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                kept.append(item)
            elif isinstance(item, str):
                kept.append(item)
            # image_url 等非文本块被丢弃
        return kept
    return content


def messages_tokens(messages: list, *, skip_summary: bool = True, skip_noise: bool = True) -> int:
    """估算消息列表的总 token 数（可选跳过摘要与噪声消息）。"""
    total = 0
    for m in messages:
        if skip_summary and has_summary_prefix(m):
            continue
        if skip_noise and is_noise_message(m):
            continue
        total += estimate_tokens(_content_text(getattr(m, "content", "")))
    return total


def truncate_messages_to_budget(
    messages: list,
    budget_tokens: int = DEFAULT_KEEP_TOKENS,
    *,
    skip_summary: bool = False,
    skip_noise: bool = False,
) -> tuple[list, int]:
    """把消息列表截断到 token 预算内，保留**最近**的完整消息。

    从最老的消息开始丢弃，直到剩余消息（含摘要前缀）总 token 数不超过预算。
    摘要消息本身不再二次截断（skip_summary 默认 False，摘要计入预算但不被丢弃）。

    Returns:
        (截断后的消息列表, 被丢弃的消息条数)。若无截断返回原列表与 0。
    """
    if budget_tokens <= 0:
        return list(messages), 0

    # 从尾部累加，找到能完整容纳进预算的最早起点
    kept = list(messages)
    dropped = 0
    while kept:
        total = sum(
            estimate_tokens(_content_text(getattr(m, "content", "")))
            for m in kept
            if (not skip_summary or not has_summary_prefix(m))
            and (not skip_noise or not is_noise_message(m))
        )
        if total <= budget_tokens:
            break
        # 丢弃最老的一条
        kept.pop(0)
        dropped += 1
    return kept, dropped
