"""Agent 共享工具函数 — 纯函数，无副作用，便于测试。"""

from langchain_core.messages import HumanMessage


def extract_user_text(messages: list) -> str:
    """从消息列表中提取最近一条用户消息文本（已清理 lone surrogates）。

    遍历消息列表（反向），返回第一条 HumanMessage 的内容。
    如果找不到用户消息则返回空字符串。
    """
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and m.content:
            raw = str(m.content)
            return raw.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
    return ""


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
