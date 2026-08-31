"""Agent 共享工具函数 — 纯函数，无副作用，便于测试。"""

from langchain_core.messages import HumanMessage


def content_to_text(content) -> str:
    """把消息 content（str 或 LangChain 多模态 list）转成纯文本。

    多模态 content 是 list[dict]，含 ``{"type":"text","text":...}`` 与
    ``{"type":"image_url","image_url":...}`` 等块；只拼接 text 块、丢弃图片块
    （图片块对意图分类 / 语义缓存键 / 查询改写等纯文本逻辑无意义，且 ``str()``
    会产生带 base64 的 Python repr）。
    """
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


def extract_user_text(messages: list) -> str:
    """从消息列表中提取最近一条用户消息文本（已清理 lone surrogates）。

    遍历消息列表（反向），返回第一条 HumanMessage 的内容（多模态消息
    只取文本块，见 content_to_text）。如果找不到用户消息则返回空字符串。
    """
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and m.content:
            raw = content_to_text(m.content)
            return raw.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
    return ""


def messages_have_image(messages: list) -> bool:
    """检查消息列表中是否存在图片块（多模态输入）。

    语义缓存以 user_text 为键，图片问答若只按文本缓存会跨图串味；调用方
    检测到图片时应跳过缓存读写。
    """
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image_url":
                    return True
    return False


def is_tool_error(msg) -> bool:
    """判断一条 ToolMessage 是否表示工具执行错误。

    支持三种错误格式：
    - dict:  {"status": "error", ...}
    - JSON string: '{"status": "error", ...}'
    - 内联错误模式: 字符串中含 '"status": "error"' / '"status":"error"'
    - list:  任一元素为 dict 且 status=="error"
    """
    if not hasattr(msg, "content"):
        return False
    content = msg.content
    if isinstance(content, dict):
        return content.get("status") == "error"
    if isinstance(content, str):
        import json
        try:
            parsed = json.loads(content)
            return isinstance(parsed, dict) and parsed.get("status") == "error"
        except (json.JSONDecodeError, TypeError):
            pass
        return '"status": "error"' in content or '"status":"error"' in content
    if isinstance(content, list):
        return any(isinstance(i, dict) and i.get("status") == "error" for i in content)
    return False


def extract_first_json(text: str) -> dict | None:
    """从文本中提取第一个完整 JSON 对象（支持嵌套和 LLM 尾注）。

    从第一个 '{' 开始，计数大括号匹配找到完整的 JSON 对象。
    如果无法找到或解析失败则返回 None。
    """
    import json as _json
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    end = start
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if depth != 0:
        return None
    try:
        return _json.loads(text[start:end])
    except (_json.JSONDecodeError, TypeError):
        return None


def build_memory_injection(memories: list, limit: int = 20) -> str:
    """从 Mem0 记忆列表构建 System Prompt 注入文本。

    用于 CLI 启动加载和用户切换时注入已存储的用户记忆。
    返回格式化的 Markdown 字符串，无记忆时返回空字符串。
    """
    if not memories:
        return ""
    parts = ["## 当前用户记忆（Mem0 语义存储）"]
    for m in memories[:limit]:
        text = m.get("memory", "")
        if text:
            parts.append(f"- {text}")
    return "\n".join(parts)


def truncate_tool_result(content, max_len: int = 500) -> str:
    """截断工具调用结果，避免在终端输出中刷屏。

    支持 str / dict / list 三种类型，超过 max_len 时追加省略号。
    """
    text = str(content)
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
