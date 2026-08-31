"""Agent 核心 — LLM 适配 + 工具定义 + System Prompt。"""

import asyncio

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool

from config import LLM_API_KEY, LLM_BASE_URL
from rag import async_search_knowledge, async_index_knowledge, async_list_sources
from skills import get_skill_tools
from .context_budget import DEFAULT_CONTEXT_WINDOW_TOKENS
from .rpa_jobs import get_submit_rpa_tools



# ---------------------------------------------------------------------------
# LLM 工厂
# ---------------------------------------------------------------------------


def create_llm(
    model: str,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    base_url: str | None = None,
) -> ChatOpenAI:
    """创建 LLM 实例，按 API Key 前缀自动适配认证方式。

    tp-* 前缀 → 自定义 api-key 头认证 + 支持服务端 web_search 工具；
    sk-* 前缀 → 标准 OpenAI Bearer 认证。

    内建容错：max_retries=3（exponential backoff），request_timeout=120s。
    """
    uses_custom_auth = LLM_API_KEY.startswith("tp-")
    url = base_url or LLM_BASE_URL
    is_deepseek = "deepseek" in url

    kwargs: dict = {
        "model": model,
        "base_url": url,
        "temperature": temperature,
        "use_responses_api": False,
        "max_retries": 3,
        "request_timeout": 120.0,
    }
    if uses_custom_auth:
        kwargs["default_headers"] = {"api-key": LLM_API_KEY}
        kwargs["openai_api_key"] = "not-used"
    else:
        kwargs["openai_api_key"] = LLM_API_KEY

    # DeepSeek v4 默认开启 thinking 模式，多轮工具调用需要回传 reasoning_content，
    # 但 LangGraph 消息序列化流程中 reasoning_content 容易丢失导致 400 错误。
    # 关闭 thinking 模式避免此问题，同时也能降低 TTFT。
    if is_deepseek:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)


def create_llm_with_fallback(
    model: str,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """创建 LLM 实例，支持主模型 → 备用模型1 → 备用模型2 的三级回退。

    回退策略：
        1. 主模型不可用 → 尝试 LLM_FALLBACK_MODEL
        2. 备用1也不可用 → 尝试 LLM_FALLBACK_MODEL_2
        3. 全部失败 → 返回主模型实例（让调用方收到原始错误）

    此函数只负责创建 LLM 实例，运行时 API 错误的回退在 call_model 中处理。
    """
    from config import LLM_FALLBACK_MODEL, LLM_FALLBACK_MODEL_2
    import logging
    _log = logging.getLogger(__name__)

    # 尝试创建主模型
    try:
        return create_llm(model=model, temperature=temperature, max_tokens=max_tokens)
    except Exception as e:
        _log.warning("无法创建主模型 %s: %s，尝试回退...", model, e)

    # 回退 1
    if LLM_FALLBACK_MODEL:
        try:
            _log.info("回退到备用模型 1: %s", LLM_FALLBACK_MODEL)
            return create_llm(model=LLM_FALLBACK_MODEL, temperature=temperature, max_tokens=max_tokens)
        except Exception as e:
            _log.warning("备用模型 1 %s 也不可用: %s", LLM_FALLBACK_MODEL, e)

    # 回退 2
    if LLM_FALLBACK_MODEL_2:
        try:
            _log.info("回退到备用模型 2: %s", LLM_FALLBACK_MODEL_2)
            return create_llm(model=LLM_FALLBACK_MODEL_2, temperature=temperature, max_tokens=max_tokens)
        except Exception as e:
            _log.warning("备用模型 2 %s 也不可用: %s", LLM_FALLBACK_MODEL_2, e)

    # 全部失败
    raise RuntimeError(f"所有模型均不可用: {model}, {LLM_FALLBACK_MODEL}, {LLM_FALLBACK_MODEL_2}")


WEB_SEARCH_TOOL = {
    "type": "web_search",
    "max_keyword": 3,
    "limit": 3,
    "user_location": {
        "type": "approximate",
        "country": "China",
    },
}


# ---------------------------------------------------------------------------
# 核心工具定义
# ---------------------------------------------------------------------------


@tool
async def tool_search_knowledge(query: str, top_k: int = 5, mode: str = "dense") -> dict:
    """从知识库中语义检索相关内容。

当用户的问题需要参考资料或已索引的文档时，使用此工具搜索知识库。
检索流程：向量粗排 → Cross-Encoder 重排精排 → 返回 Top-K。

    Args:
        query: 搜索查询文本。
        top_k: 返回的最大分块数（默认 5）。
        mode:  检索模式，"dense"=语义检索（默认），"hybrid"=BM25+向量混合检索。
    """
    return await async_search_knowledge(query, top_k=top_k, mode=mode)


@tool
async def tool_index_knowledge(
    text: str = "",
    source: str = "",
    tags: str = "",
    file_path: str = "",
    url: str = "",
) -> dict:
    """将文本/文件/网页分块后存入知识库，建立可检索的知识索引。

当用户提供有价值的信息、文档或要求记住某些内容时使用。
文本会被自动处理，embedding 后存入 Qdrant 并通过 LLM 构建知识图谱。

    Args:
        text:      要索引的完整文本内容（与 file_path/url 三选一）。
        source:    来源标识（文件名、URL、文档标题等）。
        tags:      逗号分隔的标签（方便分类检索）。
        file_path: 本地文件路径，自动检测格式并解析（PDF/DOCX/XLSX/PPTX/TXT）。
        url:       网页 URL，自动抓取并提取正文后索引。
    """
    # 文件索引（parse_file 为同步阻塞 I/O，放入线程池避免阻塞事件循环）
    if file_path:
        try:
            import asyncio as _aio
            from rag.parsers import parse_file
            doc, chunks = await _aio.to_thread(parse_file, file_path)
            source = source or doc.metadata.get("filename", file_path)
            # 逐块索引
            indexed = 0
            for ch in chunks:
                await async_index_knowledge(ch.text, source=source, tags=tags)
                indexed += 1
            return {"status": "success", "chunks": indexed, "source": source,
                    "file": file_path, "type": "file"}
        except Exception as exc:
            return {"status": "error", "message": f"文件索引失败: {exc}"}

    # URL 索引（parse_url 内含同步 urllib 请求，同样放入线程池）
    if url:
        try:
            import asyncio as _aio
            from rag.parsers import parse_url
            doc, chunks = await _aio.to_thread(parse_url, url)
            source = source or doc.metadata.get("final_url", url)
            indexed = 0
            for ch in chunks:
                await async_index_knowledge(ch.text, source=source, tags=tags)
                indexed += 1
            return {"status": "success", "chunks": indexed, "source": source,
                    "url": url, "type": "url"}
        except Exception as exc:
            return {"status": "error", "message": f"URL 索引失败: {exc}"}

    # 纯文本索引（保持向后兼容）
    return await async_index_knowledge(text, source=source, tags=tags)


@tool
async def tool_add_memory(content: str, category: str = "general") -> dict:
    """存入一条自然语言记忆。当用户在对话中表达偏好、个人习惯、项目背景时主动调用。

支持语义搜索，无需关心 key 名称，只需用自然语言描述事实。
例: "用户叫何山，在巧逗逗做运营，喜欢简洁的回复风格"

    Args:
        content:  要记住的自然语言描述。
        category: 记忆分类 — general / preference / fact / context。
    """
    from context import add_memory as _add
    from context import get_current_user
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _add, get_current_user(), content, category,
    )


@tool
async def tool_search_memory(query: str, limit: int = 10) -> dict:
    """语义搜索用户记忆。当用户提到之前说过的内容、偏好设置时使用。

用自然语言描述要查找的内容，无需知道当初存入时的确切 key。

    Args:
        query: 要查找的记忆描述（自然语言）。
        limit: 返回结果数上限（默认 10）。
    """
    from context import search_memory as _search
    from context import get_current_user
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _search, get_current_user(), query, limit,
    )


@tool
async def tool_forget_memory(query: str) -> dict:
    """忘记匹配的记忆。当用户说"不用记了"、"忘掉关于X的信息"时使用。

用自然语言描述要删除的内容，系统会语义匹配后删除。

    Args:
        query: 要删除的记忆描述（自然语言）。
    """
    from context import forget_memory as _forget
    from context import get_current_user
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _forget, get_current_user(), query,
    )


@tool
async def tool_list_memories(limit: int = 50) -> dict:
    """列出当前用户的所有记忆。了解已记录的用户偏好和背景信息时使用。

    Args:
        limit: 返回条数上限（默认 50）。
    """
    from context import list_memories as _list
    from context import get_current_user
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _list, get_current_user(), limit,
    )


@tool
async def tool_save_episodic_memory(
    summary: str,
    importance: float = 0.6,
    tags: str = "",
    category: str = "general",
) -> dict:
    """保存一条情景记忆（L2）。当用户做了重要决策、完成复杂任务、或对话中有值得记住的事件时主动调用。

与 tool_add_memory 的区别：
- tool_add_memory → L3 语义记忆（持久偏好/背景/事实，会去重合并）
- tool_save_episodic_memory → L2 情景记忆（一次性的关键事件/决策/任务记录）

Args:
    summary: 事件摘要（一句话，<200 字符）。
    importance: 重要性 0-1（0.3=低, 0.6=中, 0.9=高，默认 0.6）。
    tags: 逗号分隔的标签，如 "decision,deployment"。
    category: 分类 — task_completion / user_decision / tool_result / preference / general。
    """
    from context import save_episodic_memory as _save, get_current_user

    loop = asyncio.get_running_loop()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    return await loop.run_in_executor(
        None, lambda: _save(
            get_current_user(), summary,
            importance=importance, tags=tag_list, category=category,
        ),
    )


@tool
async def tool_get_conversation_context(query: str = "") -> dict:
    """获取当前用户的完整对话上下文（L2 情景记忆 + L3 语义记忆）。

应在以下时机调用：
1. 每次新对话开始时 — 了解用户背景和最近的决策/任务
2. 用户问"还记得之前xxx吗" — 检索相关历史事件
3. 需要了解用户偏好时 — 补充 L3 语义记忆

返回的上下文包含：
- 近期情景记忆（关键事件/决策/任务完成记录）
- 长期语义记忆（用户偏好/背景/事实）

Args:
    query: 可选的检索查询（用于针对性搜索，留空则返回最近的记忆）。
    """
    from context import get_conversation_context as _get_ctx, get_current_user

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _get_ctx(get_current_user(), query),
    )


@tool
async def tool_list_knowledge_sources() -> dict:
    """列出知识库中所有已索引的文档概况（含 LLM 摘要、分块数、索引时间）。

    当用户问"知识库有哪些内容/什么资料"时，优先用此工具获取全貌，
    再用 tool_search_knowledge 对感兴趣的文档做深入检索。
    """
    return await async_list_sources()


@tool
async def tool_describe_image(image: str, question: str = "") -> dict:
    """调用视觉模型理解图片内容（OCR 文字转录 + 画面描述）。

    当用户上传图片、或需要识别图片中的文字、商品、截图、表格、票据等内容时使用。
    image 支持 base64 data URI、http(s) URL 或本地文件路径。

    Args:
        image:    图片（base64 data URI / http(s) URL / 本地文件路径）。
        question: 针对图片的具体问题，留空则做完整描述 + OCR 转录。
    """
    from .vision import describe_image, DEFAULT_DESCRIBE_PROMPT
    prompt = DEFAULT_DESCRIBE_PROMPT
    if question:
        prompt = f"{question}\n（请同时完整转录图中所有文字）"
    desc = await describe_image(image, prompt=prompt)
    if not desc:
        return {"status": "error", "message": "图片理解失败或视觉模型未启用"}
    return {"status": "success", "description": desc}


# ---------------------------------------------------------------------------
# 上下文预算感知（借鉴 Codex get_context_remaining 元认知工具）
# ---------------------------------------------------------------------------

# 当前会话已消耗的 prompt token 数（由 graph 在每轮 call_model 前更新）。
# 工具在独立调用上下文中执行，无法直接读取 state，因此通过模块级变量透传
# 一个「尽力而为」的预算信号；读不到时工具返回 tokens_left=null。
_session_prompt_tokens: int | None = None
_context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS


def set_session_prompt_tokens(tokens: int | None) -> None:
    """更新当前会话已消耗的 prompt token 数（graph 每轮调用前设置）。"""
    global _session_prompt_tokens
    _session_prompt_tokens = tokens


def set_context_window_tokens(tokens: int) -> None:
    """覆盖上下文窗口大小（默认 128k，可按模型实际上下文调整）。"""
    global _context_window_tokens
    _context_window_tokens = int(tokens)


def get_context_remaining_tokens() -> int | None:
    """返回剩余可用 token 数，无法估算时返回 None。"""
    if _session_prompt_tokens is None:
        return None
    return max(0, _context_window_tokens - _session_prompt_tokens)


@tool
async def tool_get_context_remaining() -> dict:
    """查询当前上下文窗口还剩多少 token 预算。

    当对话很长、或即将继续追加大量内容前调用，用于判断是否需要收敛回答、
    主动压缩上下文或提醒用户开启新会话。剩余预算不足时优先精简回答而非继续调用工具。

    Returns:
        {"tokens_left": int | null, "context_window": int, "used_tokens": int | null}
    """
    return {
        "tokens_left": get_context_remaining_tokens(),
        "context_window": _context_window_tokens,
        "used_tokens": _session_prompt_tokens,
    }


# ---------------------------------------------------------------------------
# System Prompt（动态生成 — 基于实际注册的工具列表）
# ---------------------------------------------------------------------------


# 工具前缀 → 能力描述（按优先级排序）
_TOOL_CAPABILITY_MAP: list[tuple[str, str, str]] = [
    # (前缀, 类别名, 能力描述)
    ("tool_search_knowledge", "知识库检索", "语义检索已索引的文档/参考资料"),
    ("tool_list_knowledge_sources", "知识库概况", "列出知识库所有已索引的文档来源"),
    ("tool_describe_image", "图片理解", "视觉模型 OCR/描述图片内容"),
    ("tool_index_knowledge", "知识库入库", "将文本/文档分块后存入知识图谱"),
    ("tool_add_memory", "用户记忆", "保存用户偏好/习惯/背景"),
    ("tool_search_memory", "用户记忆", "语义搜索已存储的用户记忆"),
    ("tool_forget_memory", "用户记忆", "删除匹配的用户记忆"),
    ("tool_list_memories", "用户记忆", "列出所有用户记忆"),
    ("tool_save_episodic_memory", "用户记忆", "保存关键事件/决策的情景记忆"),
    ("tool_get_conversation_context", "用户记忆", "获取完整对话上下文（情景+语义记忆）"),
    ("tool_get_context_remaining", "上下文预算", "查询上下文窗口剩余 token 预算"),
    ("mcp_searxng", "联网搜索", "SearXNG 聚合搜索引擎（Google/DDG/Startpage）"),
    ("mcp_web_url_read", "联网搜索", "读取网页详细内容"),
    ("mcp_filesystem", "文件系统", "读写/搜索/编辑本地文件"),
    ("mcp_read_file", "文件系统", "读取文件内容"),
    ("mcp_write_file", "文件系统", "写入文件"),
    ("mcp_edit_file", "文件系统", "编辑文件"),
    ("mcp_list_directory", "文件系统", "列出目录内容"),
    ("mcp_playwright", "RPA 浏览器", "浏览器自动化操作"),
    ("mcp_rpa_", "RPA 浏览器", "紫鸟浏览器自动化操作（独立 MCP server 提供）"),
    ("mcp_get_current_time", "时间查询", "获取当前日期和时间"),
    ("mcp_sequentialthinking", "多步推理", "结构化多步骤推理分析"),
    ("mcp_docker", "容器管理", "Docker 容器/镜像管理"),
    ("execute_code", "代码执行", "安全执行 Python/Shell 代码"),
    ("rpa_", "RPA 浏览器", "紫鸟浏览器自动化操作"),
    ("submit_rpa_", "RPA 浏览器", "提交 RPA 批量任务（排队执行，立即返回 job_id）"),
]


def _build_capability_context() -> tuple[str, str]:
    """基于当前已注册的工具列表动态生成能力描述和工具名列表。

    Returns:
        (capabilities_text, tool_names_text) — 供 PromptManager 模板渲染使用。
    """
    try:
        available_tools = get_all_tools()
        available_names = {t.name for t in available_tools}
    except Exception:
        available_names = set()

    # 按类别分组
    categories: dict[str, list[str]] = {}
    tool_descriptions: dict[str, str] = {}
    for prefix, category, desc in _TOOL_CAPABILITY_MAP:
        for name in available_names:
            if name.startswith(prefix) or name == prefix:
                categories.setdefault(category, []).append(name)
                tool_descriptions[name] = desc

    # 构建动态能力列表
    capability_lines: list[str] = []
    cat_order = [
        "知识库检索", "知识库概况", "知识库入库",
        "用户记忆", "联网搜索", "文件系统",
        "RPA 浏览器", "代码执行", "多步推理",
        "时间查询", "容器管理", "图片理解",
    ]
    cat_idx = 1
    for cat in cat_order:
        tools_in_cat = categories.get(cat, [])
        if not tools_in_cat:
            continue
        desc = _next((d for p, c, d in _TOOL_CAPABILITY_MAP if c == cat and any(
            t.startswith(p) or t == p for t in tools_in_cat)), "")
        tool_list = ", ".join(tools_in_cat[:8])
        if len(tools_in_cat) > 8:
            tool_list += f" 等 {len(tools_in_cat)} 个工具"
        capability_lines.append(f"{cat_idx}. {desc}: {tool_list}")
        cat_idx += 1

    capabilities = "\n".join(capability_lines) if capability_lines else "（请使用已注册的工具完成用户请求）"
    tool_names = ", ".join(sorted(available_names)) if available_names else "（无）"

    return capabilities, tool_names


def get_system_prompt(prompt_version: str | None = None) -> str:
    """基于当前已注册的工具列表动态生成 System Prompt。

    优先使用 PromptManager 从模板渲染，模板缺失时回退到硬编码版本。

    Args:
        prompt_version: 提示词版本号（如 "1.1.0"），None 使用最新版本。
                       可通过环境变量 PROMPT_VERSION 覆盖。
    """
    import os

    capabilities, tool_names = _build_capability_context()

    # 如果未指定版本，尝试从环境变量读取
    if prompt_version is None:
        prompt_version = os.getenv("PROMPT_VERSION") or None

    # 尝试从 PromptManager 加载模板
    try:
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        result = pm.render(
            "system_prompt",
            version=prompt_version,
            capabilities=capabilities,
            tool_names=tool_names,
        )
        return result
    except (ValueError, Exception):
        pass

    # 回退：硬编码提示词（兼容性保证，template 文件缺失时仍然可用）
    return (
        "你是一个能力全面的 AI 助手。\n\n"
        "## 用户记忆规则（最高优先级，必须遵守）\n"
        "1. **用户分享任何个人信息时，必须立即调用 tool_add_memory 保存**（L3 语义记忆），包括但不限于：\n"
        "   - 姓名、称呼、昵称\n"
        "   - 公司/组织名称、职位\n"
        "   - 偏好（回复风格、语言、格式等）\n"
        "   - 习惯、工作流程\n"
        "   - 项目背景、业务领域\n"
        "2. **每次对话开始时，先调用 tool_get_conversation_context 获取完整上下文**（L2 情景记忆 + L3 语义记忆）\n"
        "3. **对话中任何时候用户提到之前说过的事，先调用 tool_search_memory 确认**\n"
        "4. **用户做出重要决策、完成复杂任务、表达明确偏好时 → 调用 tool_save_episodic_memory 保存**（L2 情景记忆）\n"
        "   - importance=0.9: 重大决策、项目里程碑\n"
        "   - importance=0.6: 一般任务完成、偏好记录\n"
        "   - importance=0.3: 一般参考信息\n\n"
        "## 工具选择规则（最重要，优先阅读）\n"
        "- **用户个人信息（姓名/职位/公司/偏好）→ 立即调用 tool_add_memory 保存**\n"
        "- **重要决策/任务完成 → 调用 tool_save_episodic_memory 保存事件**\n"
        "- **新对话开始/了解背景 → 调用 tool_get_conversation_context 获取上下文**\n"
        "- **实时信息（新闻、天气、股价、最新动态）→ 用联网搜索工具**\n"
        "- **查看知识库概况/有哪些内容 → 用 tool_list_knowledge_sources 列出已索引的文档来源**\n"
        "- **已索引的文档/参考资料/内部知识 → 用 tool_search_knowledge** 从 LightRAG 知识图谱检索\n"
        "- **用户偏好/习惯/背景 → 用 tool_search_memory** 语义搜索记忆\n"
        "- 不确定属于哪类时，同时用知识库检索和联网搜索一起查询相关内容\n\n"
        "## 可用能力（基于实际注册的工具动态生成）\n"
        f"{capabilities}\n\n"
        "## 工作规则\n"
        "- **使用 Markdown 格式回复**：标题、列表、代码块、表格等按需使用。\n"
        "- 代码块使用三个反引号包裹并标注语言（例如 ```python）。\n"
        "- 保持回复简洁、准确、有帮助。\n"
        "- 工具返回错误时，分析原因并告诉用户。\n"
        "- **对话结束时用户信息必须已保存到记忆中。**\n"
        "- **世贸通抬头报关**：若工具返回\"未找到报关文件/未找到待办报关 Excel/未找到任何子订单\"，立即停止并如实告知用户当前没有待办报关文件，**不得**再调用其他工具（文件系统、知识库、联网搜索等）去查找报关文件。\n\n"
        "## 来源引用规则（使用知识库时必须遵守）\n"
        "- 当回答使用了知识库检索的内容时，**必须在回答末尾列出引用来源**。\n"
        "- 引用格式：[N] 来源: {文件名}（相关度: {score}）\n"
        "- 如果某个来源未提供文件名，使用 '知识库' 作为默认标识。\n"
        "- 如果检索结果不足以回答问题，**请明确说明**而不是编造内容。\n"
        "- 示例：\n"
        "  回答内容...\n\n"
        "  ---\n"
        "  **来源引用**\n"
        "  [1] 来源: product_manual.pdf（相关度: 0.92）\n"
        "  [2] 来源: faq_database.xlsx（相关度: 0.85）\n\n"
        "## 当前可用工具\n"
        "以下工具已加载到系统中，你可以直接使用：\n"
        f"{tool_names}\n"
    )


def _next(iterable, default=""):
    """返回可迭代对象的第一个元素，无元素时返回默认值。"""
    return next(iter(iterable), default)


# 兼容旧代码的模块级常量（首次访问时动态生成）
SYSTEM_PROMPT = ""  # 废弃：使用 get_system_prompt() 替代


def register_mcp_tools(tools: list) -> None:
    """注册外部 MCP 工具到 Agent 工具列表。

    工具将在 get_all_tools() 中与核心工具和 skill 工具一起返回。
    通常与 mcp_wrapper.client.import_mcp_tools() 配合使用。
    """
    from app_context import get_app_context
    get_app_context().register_mcp_tools(tools)


def get_core_tools() -> list:
    """返回核心工具列表（不含 skill 工具）。

    含 3 个 RPA 提交工具（submit_rpa_*）：LLM 只产出「哪种任务 + 参数」，
    提交即返回 job_id，由后台调度器排队执行，绝不阻塞回合。
    """
    return [
        tool_search_knowledge,
        tool_index_knowledge,
        tool_add_memory,
        tool_search_memory,
        tool_forget_memory,
        tool_list_memories,
        tool_save_episodic_memory,
        tool_get_conversation_context,
        tool_list_knowledge_sources,
        tool_describe_image,
        tool_get_context_remaining,
        *get_submit_rpa_tools(),
    ]


def get_all_tools() -> list:
    """返回全部工具：核心工具 + 已注册 skill 工具 + 外部 MCP 工具。"""
    from app_context import get_app_context
    return get_core_tools() + get_skill_tools() + get_app_context().get_mcp_tools()
