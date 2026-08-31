"""视觉模型客户端 — 图片理解统一入口。

deepseek-v4-flash-vision-exp（OpenAI 兼容）：文本+图片进 → 文本出。
支持 base64 data URI / 本地路径 / HTTP(S) URL 三种输入，失败或禁用时
返回空字符串由调用方降级，绝不让视觉异常打断主链路。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认描述提示词：优先转录图中文字（OCR），再概括视觉内容。
DEFAULT_DESCRIBE_PROMPT = (
    "请详细描述这张图片的内容。如果图中包含文字（截图、表格、票据、商品信息等），"
    "请完整、准确地转录所有文字；再补充对画面、数据或结构的简要说明。"
)


def _encode_image_file(path: str | Path) -> str:
    """本地图片文件 → base64 data URI。"""
    p = Path(path)
    mime, _ = mimetypes.guess_type(str(p))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _normalize_image(image: str | Path) -> dict:
    """把各种图片输入规范成 OpenAI 多模态 image_url 结构。"""
    s = str(image).strip()
    if s.startswith("data:image/"):
        return {"type": "image_url", "image_url": {"url": s}}
    if s.startswith(("http://", "https://")):
        return {"type": "image_url", "image_url": {"url": s}}
    p = Path(image)
    if p.is_file():
        return {"type": "image_url", "image_url": {"url": _encode_image_file(p)}}
    # 兜底：当作 data URI / URL 原样透传
    return {"type": "image_url", "image_url": {"url": s}}


async def describe_image(
    image: str | Path,
    prompt: str = DEFAULT_DESCRIBE_PROMPT,
    model: str | None = None,
) -> str:
    """调用视觉模型描述/OCR 一张图片，返回文本。

    Args:
        image: base64 data URI / 本地路径 / HTTP(S) URL。
        prompt: 描述指令，默认 OCR 优先 + 结构说明。
        model: 覆盖默认视觉模型（测试用）。

    Returns:
        描述文本；禁用、无 key 或调用失败时返回空字符串（调用方降级）。
    """
    from config import VISION_ENABLED, VISION_MODEL, VISION_API_KEY
    if not VISION_ENABLED:
        return ""
    if not VISION_API_KEY:
        logger.warning("视觉模型未配置 API Key，跳过图片理解")
        return ""

    from .client_factory import get_vision_client
    image_part = _normalize_image(image)
    try:
        client = get_vision_client()
        resp = await client.chat.completions.create(
            model=model or VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    image_part,
                ],
            }],
            max_tokens=1000,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        logger.warning(
            "视觉模型调用失败，图片 %s 降级为无描述",
            str(image)[:60],
            exc_info=True,
        )
        return ""


async def enrich_image_chunks(parsed_doc, chunks: list, prompt: str | None = None) -> list:
    """图片文档入库前增强：用视觉模型描述替换占位符分块文本。

    图片解析（parsers._parse_image）只在 metadata 存 base64、text 放占位符
    ``[IMAGE: name]``。此函数在异步入库上下文里调用视觉模型拿到描述，
    把描述文本（含文件名标签）写回首个分块，供 embedding/检索使用。

    Args:
        parsed_doc: rag.parsers.ParsedDocument（含 metadata.image_base64）。
        chunks: parsed_doc.to_chunks() 的分块列表。
        prompt: 覆盖默认描述指令。

    Returns:
        增强后的 chunks；非图片或视觉失败时原样返回（降级）。
    """
    metadata = getattr(parsed_doc, "metadata", {}) or {}
    image_uri = metadata.get("image_base64")
    if not image_uri:
        return chunks

    filename = metadata.get("filename", "") or "image"
    desc = await describe_image(image_uri, prompt=prompt or DEFAULT_DESCRIBE_PROMPT)
    if not desc:
        logger.info("图片 %s 视觉描述为空，按占位符文本入库", filename)
        return chunks

    label = f"[图片: {filename}]\n{desc}"
    if chunks:
        chunks[0].text = label
    else:
        from rag.parsers import Chunk
        chunks.append(Chunk(text=label, metadata={"chunk_type": "image", **metadata}))
    return chunks
