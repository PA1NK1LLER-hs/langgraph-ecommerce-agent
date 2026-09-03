"""对话路由 — WebSocket 流式聊天 + 对话历史。"""
import asyncio as _asyncio
import json
import logging
import os
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from langchain_core.messages import HumanMessage

from ..database import get_db
from ..deps import get_ws_user, get_user_by_token
from ..rate_limit import ws_limiter, check_ws_rate
from ..routers.threads import ensure_thread, auto_title_thread
from agent.graph import get_agent
from agent.progress import CURRENT_THREAD_ID
from agent.utils import build_memory_injection
from auth.permissions import get_user_role
from config import display_model_name
from context import load_user_context

logger = logging.getLogger("api.chat")
router = APIRouter()

# 客户端断连后仍在后台跑完的流任务（保证 checkpoint 落盘，历史对话可恢复）
_DETACHED_STREAMS: set = set()


async def _safe_send(websocket: WebSocket, payload: dict) -> None:
    """发送 WS 事件；客户端已断连时静默丢弃，不中断流消费。

    关键：若 send 抛异常导致 async for 退出，astream 生成器被 aclose，
    正在执行的 agent 节点会收到 CancelledError，回复将无法写入 checkpoint
    （表现为历史对话只有用户消息）。因此发送失败必须吞掉，让图运行到完成。
    """
    try:
        await websocket.send_json(payload)
    except Exception:
        logger.debug("WS send failed (client disconnected), stream continues for checkpoint")


# ── 审批等待超时（秒）──
try:
    _APPROVAL_TIMEOUT = int(os.getenv("APPROVAL_TTL_SECONDS", "300"))
except (ValueError, TypeError):
    _APPROVAL_TIMEOUT = 300


async def _wait_for_approval_decision(websocket: WebSocket, thread_id: str) -> str:
    """等待前端通过 WebSocket 发送审批决定。

    超时后自动返回 "deny"（安全默认值）。
    也支持通过 REST API (/api/approvals/{thread_id}) 触发恢复。
    """
    try:
        raw = await _asyncio.wait_for(websocket.receive_text(), timeout=_APPROVAL_TIMEOUT)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON during approval wait, defaulting to deny")
            return "deny"
        if data.get("type") == "approval_decision":
            decision = data.get("decision", "deny")
            if decision in ("approve", "deny"):
                logger.info("Approval decision received via WS: %s", decision)
                return decision
        # 非审批消息 → 默认拒绝（保存消息内容用于后续恢复）
        logger.warning("Unexpected message during approval wait, defaulting to deny")
        return "deny"
    except _asyncio.TimeoutError:
        logger.warning("Approval wait timed out after %ds, defaulting to deny", _APPROVAL_TIMEOUT)
        await _safe_send(websocket, {
            "type": "error",
            "content": f"审批超时（{_APPROVAL_TIMEOUT}秒），操作已自动取消",
        })
        return "deny"
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected during approval wait for thread %s", thread_id[:8])
        return "deny"


def _extract_interrupt(node_name: str, node_output) -> list | None:
    """从 stream 块中提取中断信息，兼容 langgraph 0.x 和 1.x。

    langgraph 1.x: node_name == "__interrupt__", node_output == (Interrupt(...),)
    langgraph 0.x: node_name == "check_approval", node_output == {"__interrupt__": [...]}
    """
    if node_name == "__interrupt__":
        # langgraph 1.x: node_output 是 Interrupt 对象的元组
        if isinstance(node_output, (list, tuple)):
            return list(node_output)
        return [node_output] if node_output is not None else None
    if isinstance(node_output, dict) and "__interrupt__" in node_output:
        # langgraph 0.x 向后兼容
        info = node_output["__interrupt__"]
        if isinstance(info, (list, tuple)):
            return list(info)
        return [info] if info is not None else None
    return None


async def _interrupt_still_pending(agent, config: dict) -> bool:
    """检查中断是否仍处于挂起状态（未被 REST API 恢复）。"""
    try:
        state = await agent.aget_state(config)
        if state is None:
            return False
        interrupts = getattr(state, "interrupts", []) or []
        return len(interrupts) > 0
    except Exception:
        return True  # 无法验证时保守地认为仍在挂起


async def _stream_resume(agent, command, config: dict, websocket: WebSocket, thread_id: str = "") -> None:
    """流式恢复执行并处理可能出现的第二个中断。"""
    from langgraph.types import Command

    async for chunk in agent.astream(
        command,
        config=config,
        stream_mode=["updates", "messages"],
    ):
        mode, payload = chunk
        if mode == "messages":
            msg, _meta = payload
            if msg.type in ("ai", "AIMessageChunk") and msg.content:
                await _safe_send(websocket, {
                    "type": "text",
                    "content": msg.content,
                    "model": display_model_name(getattr(msg, "additional_kwargs", {}).get("_model_used", "")),
                })
            continue
        for node_name, node_output in payload.items():
            # 处理恢复期间可能的第二次中断
            interrupt_info = _extract_interrupt(node_name, node_output)
            if interrupt_info is not None:
                interrupt_value = getattr(interrupt_info[0], "value", interrupt_info[0]) if interrupt_info else None
                if isinstance(interrupt_value, dict) and interrupt_value.get("type") == "approval_required":
                    logger.info("Second approval interrupt during resume, auto-denying")
                    # 嵌套中断：自动拒绝以避免无限等待
                    from langgraph.types import Command as Cmd2
                    await _stream_resume(agent, Cmd2(resume="deny"), config, websocket, thread_id)
                    continue
            await _send_node_output(websocket, node_name, node_output)


async def _run_turn(agent, input_state: dict, config: dict, websocket: WebSocket, thread_id: str) -> None:
    """执行一轮 agent 流式生成并把事件推给前端。

    先把当前对话 thread_id 置入 CURRENT_THREAD_ID contextvar：本函数由 ws_chat
    经 create_task 在独立 task 运行，与 agent.astream 内所有协程同一 asyncio
    context，RPA submit/query 工具经 await tool.ainvoke 同任务执行可读到该值，
    据此记录并解析「本线程最近提交的 RPA 任务」（结果回流链路，见 rpa_jobs）。
    回合结束在 finally 复位，避免泄漏到下一轮。
    """
    token = CURRENT_THREAD_ID.set(thread_id)
    try:
        await _run_turn_impl(agent, input_state, config, websocket, thread_id)
    finally:
        CURRENT_THREAD_ID.reset(token)


async def _run_turn_impl(agent, input_state: dict, config: dict, websocket: WebSocket, thread_id: str) -> None:
    """执行一轮 agent 流式生成并把事件推给前端。

    发送失败（客户端断连）不中断流消费：图必须运行到完成，
    否则正在执行的节点被取消，回复写不进 checkpoint（历史对话丢失）。
    """
    try:
        async for chunk in agent.astream(
            input_state, config=config, stream_mode=["updates", "messages"],
        ):
            mode, payload = chunk

            if mode == "messages":
                msg, _meta = payload
                if msg.type in ("ai", "AIMessageChunk") and msg.content:
                    await _safe_send(websocket, {
                        "type": "text",
                        "content": msg.content,
                        "model": display_model_name(getattr(msg, "additional_kwargs", {}).get("_model_used", "")),
                    })
                continue

            for node_name, node_output in payload.items():
                # ── Human-in-the-Loop: 审批中断检测 ──
                interrupt_info = _extract_interrupt(node_name, node_output)
                if interrupt_info is not None:
                    interrupt_value = getattr(interrupt_info[0], "value", interrupt_info[0]) if interrupt_info else None
                    if isinstance(interrupt_value, dict) and interrupt_value.get("type") == "approval_required":
                        logger.info("Approval interrupt detected for thread %s", thread_id[:8])
                        await _safe_send(websocket, interrupt_value)
                        # 检查是否已被 REST API 恢复
                        if not await _interrupt_still_pending(agent, config):
                            logger.info("Interrupt already resolved via REST for thread %s", thread_id[:8])
                            continue
                        # 等待前端发送审批决定
                        decision = await _wait_for_approval_decision(websocket, thread_id)
                        # 用 Command(resume=...) 恢复执行
                        from langgraph.types import Command
                        await _stream_resume(agent, Command(resume=decision), config, websocket, thread_id)
                        continue  # 恢复后的流已处理完毕

                await _send_node_output(websocket, node_name, node_output)

        await _safe_send(websocket, {"type": "done"})
    except _asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Stream error for thread %s", thread_id[:8])
        await _safe_send(websocket, {"type": "error", "content": str(exc)[:500]})


async def _send_node_output(websocket: WebSocket, node_name: str, node_output: dict) -> None:
    """将图节点输出转换为 WebSocket 事件发送给前端。

    从 ws_chat 主循环中提取，避免代码重复。
    注意：AI 文本内容由 messages 流模式按 token 级别实时推送，
    此处仅处理结构化事件、计划、工具调用和工具结果。
    """
    if not isinstance(node_output, dict):
        return

    # ── 子代理事件（真 Sub-Agent）──
    # supervisor 委派 → specialist_started（前端显示"研究员 开始执行"运行中 chip）
    started = node_output.get("specialist_started")
    if started and isinstance(started, dict):
        await _safe_send(websocket, {
            "type": "specialist_started",
            "specialist": started.get("specialist", ""),
            "name": started.get("name", ""),
            "icon": started.get("icon", ""),
        })

    # run_specialist 子图执行完成 → specialist_result（前端把 chip 标记为完成 + 摘要）
    sub_report = node_output.get("specialist_report")
    if sub_report and isinstance(sub_report, dict):
        await _safe_send(websocket, {
            "type": "specialist_result",
            "specialist": sub_report.get("specialist", ""),
            "name": sub_report.get("name", ""),
            "icon": sub_report.get("icon", ""),
            "report": (sub_report.get("report", "") or "")[:2000],
        })

    # ── 结构化输出事件 ──
    if node_output.get("structured_response"):
        await _safe_send(websocket, {
            "type": "structured_output",
            "content": node_output["structured_response"],
        })

    if node_name == "planner" and node_output.get("plan"):
        await _safe_send(websocket, {"type": "plan", "content": node_output["plan"]})

    msgs = node_output.get("messages", [])
    for msg in msgs:
        msg_type = getattr(msg, "type", None) or (msg.get("type") if isinstance(msg, dict) else None)
        if msg_type is None:
            continue
        if msg_type == "ai":
            # 流式 token 分块不携带 additional_kwargs，模型标记只能从完整 AI 消息
            # 中提取，补发独立 model 事件（前端收到后标注当前回复卡片）
            ak = getattr(msg, "additional_kwargs", {}) or {}
            if ak.get("_model_used"):
                await _safe_send(websocket, {
                    "type": "model",
                    "model": display_model_name(ak["_model_used"]),
                })
        if msg_type == "ai" and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                await _safe_send(websocket, {
                    "type": "tool_call",
                    "tool": tc.get("name", "?"),
                    "args": str(tc.get("args", {}))[:200],
                })
        elif msg_type == "tool":
            name = getattr(msg, "name", None) or (msg.get("name") if isinstance(msg, dict) else "?")
            content = getattr(msg, "content", "") or (msg.get("content", "") if isinstance(msg, dict) else str(msg))
            is_err = isinstance(content, dict) and content.get("status") == "error"
            is_denied = isinstance(content, dict) and content.get("status") == "denied"
            full_content = str(content)
            await _safe_send(websocket, {
                "type": "tool_result",
                "tool": str(name),
                "content": full_content[:300],
                "truncated": len(full_content) > 300,
                "error": bool(is_err),
                "denied": bool(is_denied),
            })


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    """WebSocket 流式对话端点。

    查询参数：
    - token: JWT 认证令牌
    - thread_id: 可选，继续已有对话；不传则创建新对话
    """
    await websocket.accept()

    # 获取数据库会话用于认证
    try:
        db_gen = get_db()
        db: AsyncSession = await anext(db_gen)
    except (RuntimeError, StopAsyncIteration):
        db = None

    # 向后兼容：URL query 参数携带 token（旧客户端）。
    # 新客户端将 token 作为首条消息发送（见下方 auth 分支），避免 token 泄漏进日志（F11）。
    user = None
    query_token = websocket.query_params.get("token", "")
    if db and query_token:
        user = await get_ws_user(websocket, db)

    # ── 确定 thread_id：优先使用客户端传入的，否则新建 ──
    client_thread_id = websocket.query_params.get("thread_id", "").strip()
    thread_id = client_thread_id if client_thread_id else str(uuid.uuid4())
    context_injection = ""
    session_ready = False

    async def setup_session() -> None:
        """认证完成后初始化用户上下文与线程元数据（幂等，仅执行一次）。"""
        nonlocal context_injection, session_ready
        if session_ready:
            return
        session_ready = True

        if user:
            from context.manager import set_current_user
            set_current_user(str(user.id))
            user_ctx = load_user_context(str(user.id))
            context_injection = build_memory_injection(user_ctx.get("memories", []))

            # 确保线程元数据存在（幂等）
            try:
                await ensure_thread(int(user.id), thread_id)
            except Exception:
                logger.warning("Failed to save thread metadata for user %s", user.id, exc_info=True)
        else:
            # 未认证用户也保存线程（使用虚拟 user_id=0 标识匿名会话）
            try:
                await ensure_thread(0, thread_id, title="匿名会话")
            except Exception:
                logger.debug("Failed to save anonymous thread metadata", exc_info=True)

        # 通知前端当前使用的 thread_id
        await websocket.send_json({"type": "thread_id", "content": thread_id})

    # 旧流程（query token）：立即完成认证与初始化
    if query_token:
        await setup_session()

    # ── 速率限制 key：优先用用户 ID，否则用客户端 IP ──
    rate_key = str(user.id) if user else (websocket.client[0] if websocket.client else "unknown")
    _first_message_sent = False

    try:
        agent = None  # 懒加载：认证可能在首条消息才完成，需使用最新 context_injection
        config = {"configurable": {"thread_id": thread_id}}

        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "content": "消息格式错误，请使用 JSON"})
                continue
            if not isinstance(data, dict):
                continue

            # ── 首条消息认证（新客户端）──
            if data.get("type") == "auth":
                if user is None and db:
                    user = await get_user_by_token(str(data.get("token", "")), db)
                if user is not None:
                    rate_key = str(user.id)
                await setup_session()
                await websocket.send_json({"type": "auth_ok", "content": user is not None})
                continue

            message = (data.get("message") or "").strip()
            # 可选的多模态图片：前端传 base64 data URI 或 http(s) URL 列表
            image_uris = data.get("images") or []
            if not message and not image_uris:
                continue
            if not message:
                message = "请描述/分析这张图片"

            # 懒加载 agent（认证可能刚完成，context_injection 已更新）
            if agent is None:
                agent = await get_agent(context_summary=context_injection)

            # ── 速率限制检查 ──
            allowed, retry_after = await check_ws_rate(rate_key)
            if not allowed:
                await websocket.send_json({
                    "type": "error",
                    "content": f"消息过于频繁，请 {retry_after:.0f} 秒后重试",
                })
                continue

            cmd = message.lower()
            if cmd in ("/reset", "/clear"):
                thread_id = str(uuid.uuid4())
                config = {"configurable": {"thread_id": thread_id}}
                _first_message_sent = False
                try:
                    uid = int(user.id) if user else 0
                    await ensure_thread(uid, thread_id, title="新对话")
                except Exception:
                    logger.warning("Failed to create thread for reset (thread_id=%s)", thread_id[:8])
                await websocket.send_json({"type": "thread_id", "content": thread_id})
                await websocket.send_json({"type": "system", "content": "已开启新对话"})
                await websocket.send_json({"type": "done"})
                continue

            # ── 首条消息自动设置对话标题 ──
            if not _first_message_sent and user:
                _first_message_sent = True
                try:
                    await auto_title_thread(thread_id, message[:30])
                    # 通知前端标题已更新，侧边栏可刷新
                    title_text = message.replace("\n", " ").strip()[:30]
                    await websocket.send_json({
                        "type": "title_updated",
                        "thread_id": thread_id,
                        "title": title_text,
                    })
                except Exception:
                    logger.warning("Auto-title failed for thread %s", thread_id, exc_info=True)

            # 多模态消息：文本 + 图片块（图片为空时退化为纯文本 HumanMessage）
            if image_uris:
                _content: list = [{"type": "text", "text": message}]
                for uri in image_uris:
                    _content.append({"type": "image_url", "image_url": {"url": uri}})
                user_message = HumanMessage(content=_content)
            else:
                user_message = HumanMessage(content=message)

            input_state = {
                "messages": [user_message],
                "plan": "",
                "tool_failures": 0,
                "tool_retries": 0,
                "rag_context": "",
                "response_schema": data.get("response_schema"),  # 结构化输出请求
                # 注入用户角色供 agent 做 RBAC 工具权限判断（修复：此前恒被当 viewer）
                "user_role": get_user_role(user) if user else "viewer",
            }

            # 流执行放在独立 task 中，用 shield 保护：
            # 客户端断连/刷新/切换线程时，本 handler 被取消或 send 失败，
            # 但图必须继续跑完，把回复写入 checkpoint（历史对话可恢复）。
            run_task = _asyncio.create_task(
                _run_turn(agent, input_state, config, websocket, thread_id)
            )
            try:
                await _asyncio.shield(run_task)
            except _asyncio.CancelledError:
                # handler 被取消（服务关停等）：保留后台任务继续完成 checkpoint 落盘
                _DETACHED_STREAMS.add(run_task)
                run_task.add_done_callback(_DETACHED_STREAMS.discard)
                raise

    except WebSocketDisconnect:
        pass
    finally:
        if db:
            try:
                await db.close()
            except Exception:
                pass
