/** 前后端共享的消息事件类型契约。

后端 WebSocket 推送的事件类型与前端的联合类型保持同步。
*/

import type { ToolTag } from '../utils/tools'

// ── WebSocket 事件类型 ──
export type WSEventType =
  | 'thread_id'
  | 'text'
  | 'model'
  | 'tool_call'
  | 'tool_result'
  | 'plan'
  | 'system'
  | 'error'
  | 'title_updated'
  | 'done'
  | 'approval_required'
  | 'structured_output'

// ── WebSocket 事件载荷 ──
export interface WSThreadId { type: 'thread_id'; content: string }
export interface WSText { type: 'text'; content: string; model?: string }
export interface WSModel { type: 'model'; model: string }
export interface WSToolCall { type: 'tool_call'; tool: string; args: string }
export interface WSToolResult { type: 'tool_result'; tool: string; content: string; error: boolean; denied?: boolean }
export interface WSPlan { type: 'plan'; content: string }
export interface WSSystem { type: 'system'; content: string }
export interface WSError { type: 'error'; content: string }
export interface WSTitleUpdated { type: 'title_updated'; thread_id: string; title: string }
export interface WSDone { type: 'done' }
export interface WSApprovalRequired {
  type: 'approval_required'
  calls: { id: string; name: string; args: Record<string, unknown>; risk_level: string; reason: string }[]
  message: string
}
export interface WSStructuredOutput { type: 'structured_output'; content: StructuredResponse }

// ── 结构化响应类型（与后端 Pydantic Schema 对齐）──
export type StructuredResponse =
  | TextResponse
  | TableResponse
  | ActionConfirmResponse
  | TaskPlanResponse

export interface TextResponse {
  response_type: 'text'
  content: string
}

export interface TableResponse {
  response_type: 'table'
  title: string
  columns: string[]
  rows: unknown[][]
}

export interface ActionConfirmResponse {
  response_type: 'action_confirm'
  action: string
  tool_name: string
  tool_args: Record<string, unknown>
  risk_summary: string
}

export interface TaskPlanResponse {
  response_type: 'task_plan'
  goal: string
  steps: { step: number; description: string; status: string }[]
  estimated_time: string
}

// ── UI 消息模型 ──

/** 附着在 AI 消息卡片上的工具标签（搜索/代码/RPA + 运行状态）。 */
export interface UIToolChip {
  name: string
  args?: string
  tag: ToolTag | null
  status: 'running' | 'done' | 'error' | 'denied'
  result?: string
  expanded?: boolean
}

export interface UIMessage {
  id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  model?: string
  renderedHtml?: string  // 缓存的 markdown 渲染结果（流式期间节流更新，避免 O(n²) 重解析）
  plan?: string          // planner 输出的执行计划
  tools?: UIToolChip[]   // 工具调用标签与结果
}

// ── 审批相关 ──
export interface PendingApproval {
  calls: { id: string; name: string; args: Record<string, unknown>; risk_level: string; reason: string }[]
  message: string
}

// ── WS 事件联合类型与运行时解析 ──

export type WSEvent =
  | WSThreadId
  | WSText
  | WSModel
  | WSToolCall
  | WSToolResult
  | WSPlan
  | WSSystem
  | WSError
  | WSTitleUpdated
  | WSDone
  | WSApprovalRequired
  | WSStructuredOutput

export interface WSAuthOk { type: 'auth_ok'; content: boolean }

/** 运行时校验并收窄 WS 事件（后端为 Python，字段契约由本函数单点守护）。
 *
 * 未知/畸形事件返回 null，由调用方忽略并告警，避免 any 直接流入业务逻辑。
 */
export function parseWSEvent(raw: unknown): WSEvent | WSAuthOk | null {
  if (typeof raw !== 'object' || raw === null) return null
  const d = raw as Record<string, unknown>
  switch (d.type) {
    case 'thread_id':
    case 'plan':
    case 'system':
    case 'error':
      return typeof d.content === 'string' ? (d as unknown as WSEvent) : null
    case 'title_updated':
      return typeof d.title === 'string' && typeof d.thread_id === 'string'
        ? (d as unknown as WSTitleUpdated)
        : null
    case 'text':
      return typeof d.content === 'string' ? (d as unknown as WSText) : null
    case 'model':
      return typeof d.model === 'string' ? (d as unknown as WSModel) : null
    case 'tool_call':
      return typeof d.tool === 'string' ? (d as unknown as WSToolCall) : null
    case 'tool_result':
      return typeof d.tool === 'string' && typeof d.content === 'string'
        ? (d as unknown as WSToolResult)
        : null
    case 'done':
      return { type: 'done' }
    case 'approval_required':
      return Array.isArray(d.calls) ? (d as unknown as WSApprovalRequired) : null
    case 'structured_output':
      return d.content !== undefined ? (d as unknown as WSStructuredOutput) : null
    case 'auth_ok':
      return { type: 'auth_ok', content: d.content === true }
    default:
      return null
  }
}
