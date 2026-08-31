import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ── 通用 LLM 配置（设这里，切换厂商只需改 .env）──
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_FLASH_MODEL = os.getenv("LLM_FLASH_MODEL", "")

# ── 视觉模型（多模态：文本+图片 → 文本）──
# deepseek-v4-flash-vision-exp 与主 LLM 同 base_url/key，默认复用；需要独立
# 视觉网关时单独设 VISION_API_KEY / VISION_BASE_URL。VISION_ENABLED=false 可整体关停。
VISION_ENABLED = os.getenv("VISION_ENABLED", "true").lower() in ("1", "true", "yes")
VISION_MODEL = os.getenv("VISION_MODEL", "deepseek-v4-flash-vision-exp")
VISION_API_KEY = os.getenv("VISION_API_KEY") or LLM_API_KEY
VISION_BASE_URL = os.getenv("VISION_BASE_URL") or LLM_BASE_URL


def display_model_name(key: str) -> str:
    """把内部模型路由键（flash/pro/vision）映射为 .env 配置的真实模型名。

    图内部用 "flash"/"pro" 标记由哪个模型生成了消息；对外（WS 事件、
    历史消息 API）应展示真实模型 ID，而不是内部键。
    """
    if key == "flash":
        return LLM_FLASH_MODEL or key
    if key == "pro":
        return LLM_MODEL or key
    if key == "vision":
        return VISION_MODEL or key
    return key

# ── Embedding 服务（OpenAI 兼容端点）──
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-v3")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))

# ── Rerank 服务 ──
RERANK_API_KEY = os.getenv("RERANK_API_KEY") or EMBEDDING_API_KEY
RERANK_MODEL = os.getenv("RERANK_MODEL", "qwen3-rerank")

# Qdrant (LightRAG 向量存储)
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")

# Neo4j（LightRAG 图存储）
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password123")

# LightRAG
LIGHTRAG_WORKING_DIR = os.getenv("LIGHTRAG_WORKING_DIR", str(Path(__file__).parent / "data" / "lightrag"))

# RAG 检索注入上下文的最大分块数（search_rag_node 的 top_k）。此前硬编码 5，
# 超过 5 个相关分块时会被截断（「单分块只含一条记录」的数据形态下会漏召回）。
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "10"))

# RAG 检索模式：dense=纯语义检索（LightRAG mix），hybrid=BM25 关键词 + 语义 RRF 融合。
# 2026-08-17 已切主链路到 hybrid：对精确 ASIN/SKU/产品名关键词能补 dense 漏召，
# 对「负责人→产品」类查表问题与 dense 等价（实测）。回退纯 dense 设 RAG_MODE=dense。
RAG_MODE = os.getenv("RAG_MODE", "hybrid")

# 数据目录
DATA_DIR = os.getenv("DATA_DIR", str(Path(__file__).parent / "data"))
USER_MEMORY_DIR = os.getenv("USER_MEMORY_DIR", str(Path(__file__).parent / "data" / "user_memory"))

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# 内置联网搜索（需 LLM 厂商支持服务端 web_search 工具）
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "false").lower() in ("1", "true", "yes")

# Mem0 记忆系统
MEM0_COLLECTION_NAME = os.getenv("MEM0_COLLECTION_NAME", "mem0_user_memories")
MEM0_LLM_MODEL = os.getenv("MEM0_LLM_MODEL", LLM_MODEL)
MEM0_EMBEDDING_DIMS = int(os.getenv("MEM0_EMBEDDING_DIMS", str(EMBEDDING_DIM)))

# PostgreSQL（可选 — LangGraph checkpoint 持久化）
POSTGRES_URL = os.getenv("POSTGRES_URL", "")
POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "langgraph")

# ── 模型回退 ──
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "")     # 备用模型 1
LLM_FALLBACK_MODEL_2 = os.getenv("LLM_FALLBACK_MODEL_2", "")  # 备用模型 2

# ── 双模型路由阈值（默认约 90% 请求走 Flash，Pro 只处理少数高难度请求）──
# complex 意图且用户单条输入超过该字符数，或会话消息数超过该值，才升级到 Pro
PRO_ROUTE_MIN_CHARS = int(os.getenv("PRO_ROUTE_MIN_CHARS", "1000"))
PRO_ROUTE_MIN_MESSAGES = int(os.getenv("PRO_ROUTE_MIN_MESSAGES", "30"))

# ── Redis（可选 — 限流后端 + 语义缓存）──
REDIS_URL = os.getenv("REDIS_URL", "")

# ── 安全 ──
SECURITY_LLM_GUARD = os.getenv("SECURITY_LLM_GUARD", "false").lower() in ("1", "true", "yes")
