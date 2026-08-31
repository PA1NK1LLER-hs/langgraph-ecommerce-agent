"""图片 → 文字描述转换链路测试（多模态输入 → 纯文本模型）。

验证 ``_replace_images_with_descriptions`` 的三条关键行为：
1. 有图片块时，调用视觉模型转成文字描述（替换成功）；
2. 视觉模型失败/未启用时，降级为占位文本（绝不因图片报错）；
3. 无图片消息原样返回（幂等，不触发视觉调用）。
"""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import HumanMessage, AIMessage

from agent.graph import _replace_images_with_descriptions


def _human_with_image(text: str = "这张图是什么"):
    return HumanMessage(content=[
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])


class TestReplaceImages:
    @pytest.mark.anyio
    async def test_replaces_image_with_description(self):
        msgs = [_human_with_image()]
        with patch(
            "agent.vision.describe_image",
            new=AsyncMock(return_value="这是一张红色商品图，含文字「特价」"),
        ):
            out = await _replace_images_with_descriptions(msgs, question="这张图是什么")
        assert len(out) == 1
        content = out[0].content
        assert isinstance(content, list)
        # 图片块已被替换为 text 块
        assert not any(b.get("type") == "image_url" for b in content)
        joined = " ".join(b.get("text", "") for b in content)
        assert "红色商品图" in joined

    @pytest.mark.anyio
    async def test_degraded_on_vision_failure(self):
        msgs = [_human_with_image()]
        with patch("agent.vision.describe_image", new=AsyncMock(return_value="")):
            out = await _replace_images_with_descriptions(msgs, question="x")
        content = out[0].content
        assert not any(b.get("type") == "image_url" for b in content)
        joined = " ".join(b.get("text", "") for b in content)
        assert "未能识别" in joined

    @pytest.mark.anyio
    async def test_no_image_is_idempotent(self):
        msgs = [HumanMessage(content="纯文本"), AIMessage(content="回复")]
        with patch("agent.vision.describe_image", new=AsyncMock()) as mock_desc:
            out = await _replace_images_with_descriptions(msgs, question="x")
        # 无图片 → 不调用视觉模型，原样返回
        mock_desc.assert_not_awaited()
        assert out == msgs

    @pytest.mark.anyio
    async def test_preserves_message_type(self):
        msgs = [_human_with_image()]
        with patch("agent.vision.describe_image", new=AsyncMock(return_value="desc")):
            out = await _replace_images_with_descriptions(msgs, question="x")
        assert isinstance(out[0], HumanMessage)
