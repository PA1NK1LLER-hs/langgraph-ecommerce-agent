<script setup lang="ts">
import { ref, nextTick, onMounted, onUnmounted, watch, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useThreadsStore } from '../stores/threads'
import AppIcon from '../components/AppIcon.vue'
import { getHealth, getThread } from '../api/client'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { parseWSEvent } from '../types/chat'
import type { UIMessage, UIToolChip } from '../types/chat'
import { tagForTool } from '../utils/tools'
import type { ToolTag } from '../utils/tools'

// ── Markdown 渲染配置 ──
marked.setOptions({
  breaks: true,        // 换行符转 <br>
  gfm: true,           // GitHub Flavored Markdown
})

function renderMarkdown(text: string): string {
  if (!text) return ''
  try {
    // XSS 防护交给 DOMPurify（正则白名单可被绕过，F10）
    return DOMPurify.sanitize(marked.parse(text) as string)
  } catch {
    return text.replace(/</g, '&lt;').replace(/>/g, '&gt;')
  }
}

// ── F13：流式期间节流渲染 markdown，避免每个 token 全量重解析（O(n²)）──
let renderTimer: ReturnType<typeof setTimeout> | null = null

function scheduleRender() {
  if (!currentAssistant || renderTimer) return
  renderTimer = setTimeout(() => {
    renderTimer = null
    if (currentAssistant) currentAssistant.renderedHtml = renderMarkdown(currentAssistant.content)
  }, 100)
}

function flushRender() {
  if (renderTimer) { clearTimeout(renderTimer); renderTimer = null }
  if (currentAssistant) currentAssistant.renderedHtml = renderMarkdown(currentAssistant.content)
}

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const threads = useThreadsStore()

const messages = ref<UIMessage[]>([])
const input = ref('')
const sending = ref(false)
// ── 图片附件（多模态输入）：dataUri 用于发送，name 用于缩略图展示 ──
const attachedImages = ref<{ dataUri: string; name: string }[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
const connected = ref(false)
const agentReady = ref(false)
let ws: WebSocket | null = null
let authed = false          // auth_ok 已收到（断线重连后首条消息认证）
let pendingSend: string | null = null
let msgId = 0
let currentAssistant: UIMessage | null = null
let currentThreadId = ''
let disposed = false  // 组件已卸载标记，防止卸载后重连产生僵尸 WS

// ── 审批状态 ──
const showApprovalDialog = ref(false)
const pendingApproval = ref<{
  calls: { id: string; name: string; args: Record<string, unknown>; risk_level: string; reason: string }[]
  message: string
} | null>(null)
const approvalProcessing = ref(false)

// ── 顶部状态栏 ──
const threadTitle = computed(() => threads.titleOf(currentThreadId))
const statusText = computed(() =>
  !connected.value ? '连接已断开' : agentReady.value ? '已连接' : '正在初始化…',
)
const statusColor = computed(() =>
  !connected.value ? '#FF3B30' : agentReady.value ? '#34C759' : '#FF9F0A',
)

// ── 工具标签样式：单色主题下用黑白灰 + 图标区分类型，仅状态图标保留语义色 ──
function tagClasses(_tag: ToolTag | null): string {
  return 'bg-black/5 text-ink-2'
}

// RPA 执行日志行配色：警告/错误用语义色，其余用次级文字色
function logLineClass(level?: string): string {
  if (level === 'error') return 'text-danger'
  if (level === 'warning') return 'text-warn'
  return 'text-ink-3'
}

function ensureAssistant(): UIMessage {
  if (!currentAssistant) {
    currentAssistant = { id: ++msgId, role: 'assistant', content: '', tools: [] }
    messages.value.push(currentAssistant)
  }
  return currentAssistant
}

function connect(threadId?: string) {
  if (ws) { ws.onclose = null; ws.close() }
  // 不再清空 messages：重连/换线程时由 selectThread 显式管理历史消息，
  // 断线重连后当前对话的显示内容得以保留（F4）
  sending.value = false
  authed = false
  // 换线程时丢弃未完成的流式消息引用，避免后续 text 事件追加到已离屏的对象上
  currentAssistant = null

  const token = auth.token
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  // token 不放 URL query（会泄漏进服务器/代理日志），改为连接后首条消息认证（F11）
  let wsUrl = `${proto}//${location.host}/ws/chat`
  if (threadId) wsUrl += `?thread_id=${threadId}`

  ws = new WebSocket(wsUrl)

  ws.onopen = () => {
    connected.value = true
    if (token) ws!.send(JSON.stringify({ type: 'auth', token }))
  }
  ws.onclose = () => {
    connected.value = false
    authed = false
    sending.value = false
    currentAssistant = null
    if (pendingSend) {
      pendingSend = null
      messages.value.push({ id: ++msgId, role: 'system', content: '发送失败：连接已断开，正在自动重连…' })
      scrollDown()
    }
    // 组件已卸载（登出/路由离开）时不再重连，避免僵尸匿名会话（F3）
    if (disposed) return
    setTimeout(() => { if (!disposed) connect(currentThreadId) }, 3000)
  }
  ws.onerror = () => { connected.value = false }

  ws.onmessage = (e) => {
    let d: ReturnType<typeof parseWSEvent>
    try {
      d = parseWSEvent(JSON.parse(e.data))
    } catch {
      console.warn('[ws] 非 JSON 消息，已忽略')
      return
    }
    if (!d) {
      console.warn('[ws] 未知事件格式，已忽略:', String(e.data).slice(0, 200))
      return
    }
    switch (d.type) {
      case 'auth_ok':
        // 首条消息认证失败 → 令牌过期/无效，登出回登录页（F9/F11）
        if (!d.content) {
          auth.logout()
          router.push('/login')
          return
        }
        authed = true
        if (pendingSend) {
          const t = pendingSend
          pendingSend = null
          ws!.send(JSON.stringify({ message: t }))
        }
        break
      case 'thread_id':
        currentThreadId = d.content
        if (!threadId && currentThreadId) router.replace(`/chat/${currentThreadId}`)
        break
      case 'text':
        {
          const msg = ensureAssistant()
          if (!msg.model && d.model) msg.model = d.model  // 兜底：token 事件可能携带模型名
          msg.content += d.content
          scheduleRender()  // 节流渲染，模板读 renderedHtml
          scrollDown()
        }
        break
      case 'model':
        // 流式分块不携带 additional_kwargs，后端从节点输出补发模型事件
        if (currentAssistant && !currentAssistant.model) currentAssistant.model = d.model
        break
      case 'tool_call':
        ensureAssistant().tools!.push({
          name: d.tool,
          args: d.args,
          tag: tagForTool(d.tool),
          status: 'running',
        })
        scrollDown()
        break
      case 'tool_result': {
        // 工具结果附着到当前 AI 卡片上的工具标签（状态 + 结果）
        const tools = ensureAssistant().tools!
        const chip = tools.find(c => c.name === d.tool && c.status === 'running')
        const status: UIToolChip['status'] = d.error ? 'error' : d.denied ? 'denied' : 'done'
        if (chip) {
          chip.status = status
          chip.result = d.content
          chip.truncated = d.truncated
        } else {
          tools.push({ name: d.tool, tag: tagForTool(d.tool), status, result: d.content, truncated: d.truncated })
        }
        scrollDown()
        break
      }
      case 'rpa_log': {
        // RPA 执行日志实时附着到当前运行中的工具标签，展开显示，页面跟随滚动
        const tools = ensureAssistant().tools!
        let chip = [...tools].reverse().find(c => c.status === 'running')
        if (!chip) {
          // 日志先于 tool_call 到达的兜底：挂一个通用 RPA 标签
          chip = { name: 'RPA 执行', args: '', tag: tagForTool('rpa'), status: 'running', logs: [] }
          tools.push(chip)
        }
        chip.logs = chip.logs || []
        chip.logs.push({ content: d.content, level: d.level, time: d.time })
        if (chip.logs.length > 300) chip.logs = chip.logs.slice(-300)  // 防止超长日志撑爆 DOM
        chip.expanded = true
        scrollDown()
        break
      }
      case 'plan':
        if (currentAssistant) currentAssistant.plan = d.content
        else messages.value.push({ id: ++msgId, role: 'system', content: d.content })
        scrollDown()
        break
      case 'system':
        messages.value.push({ id: ++msgId, role: 'system', content: d.content })
        scrollDown()
        break
      case 'error':
        // 若占位气泡仍为空（尚未收到任何 token/工具），移除，避免残留空白 AI 卡片
        if (currentAssistant && !currentAssistant.content && !(currentAssistant.tools?.length)) {
          const idx = messages.value.indexOf(currentAssistant)
          if (idx >= 0) messages.value.splice(idx, 1)
        }
        messages.value.push({ id: ++msgId, role: 'system', content: d.content })
        currentAssistant = null
        sending.value = false
        scrollDown()
        break
      case 'title_updated':
        threads.upsert({ thread_id: d.thread_id, title: d.title })
        break
      case 'approval_required':
        // Human-in-the-Loop: 高风险操作需要确认
        sending.value = false
        pendingApproval.value = {
          calls: d.calls || [],
          message: d.message || '',
        }
        showApprovalDialog.value = true
        break
      case 'structured_output':
        // 结构化输出（表格/任务计划等）：提取标题作为系统消息展示（F8）
        {
          const c = d.content
          const title =
            ('title' in c && c.title) ||
            ('goal' in c && c.goal) ||
            ('content' in c && typeof c.content === 'string' && c.content) ||
            ''
          if (title) {
            messages.value.push({ id: ++msgId, role: 'system', content: String(title).slice(0, 300) })
            scrollDown()
          }
        }
        break
      case 'specialist_started': {
        // 真子代理开始执行：挂一个"运行中"的 specialist chip（脉冲动画 + 可展开）
        ensureAssistant().tools!.push({
          name: d.name || d.specialist,
          tag: tagForTool('specialist'),
          status: 'running',
          expanded: true,
        })
        scrollDown()
        break
      }
      case 'specialist_result': {
        // 子代理子图执行完成：把对应 chip 标记为 done + 附报告摘要（正文已由 text 事件流式输出）
        const tools = ensureAssistant().tools!
        const chipName = d.name || d.specialist
        const chip = tools.find(c => c.name === chipName && c.status === 'running')
        const summary = d.report.slice(0, 200)
        if (chip) {
          chip.status = 'done'
          chip.result = summary
        } else {
          tools.push({ name: chipName, tag: tagForTool('specialist'), status: 'done', result: summary })
        }
        scrollDown()
        break
      }
      case 'done':
        flushRender()  // 收尾渲染最终 markdown
        currentAssistant = null
        sending.value = false
        threads.loadThreads()  // 标题/排序可能已变化
        break
    }
  }
}

function approveAction() {
  if (!ws || !pendingApproval.value) return
  approvalProcessing.value = true
  ws.send(JSON.stringify({ type: 'approval_decision', decision: 'approve' }))
  showApprovalDialog.value = false
  pendingApproval.value = null
  sending.value = true
  approvalProcessing.value = false
}

function denyAction() {
  if (!ws || !pendingApproval.value) return
  approvalProcessing.value = true
  ws.send(JSON.stringify({ type: 'approval_decision', decision: 'deny' }))
  showApprovalDialog.value = false
  pendingApproval.value = null
  sending.value = true
  approvalProcessing.value = false
}

function pushUser(text: string, images?: string[]) {
  messages.value.push({ id: ++msgId, role: 'user', content: text, images })
  input.value = ''
  attachedImages.value = []
  if (fileInput.value) fileInput.value.value = ''
  sending.value = true
  scrollDown()
}

// ── 图片附件：本地文件 → base64 data URI ──
function readFileAsDataUri(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.readAsDataURL(file)
  })
}

async function onAttachImages(files: FileList | null) {
  if (!files || !files.length) return
  for (const file of Array.from(files)) {
    if (!file.type.startsWith('image/')) continue
    if (file.size > 10 * 1024 * 1024) {  // 单图 10MB 上限
      messages.value.push({ id: ++msgId, role: 'system', content: `图片 ${file.name} 超过 10MB，已跳过` })
      continue
    }
    try {
      const dataUri = await readFileAsDataUri(file)
      attachedImages.value.push({ dataUri, name: file.name })
    } catch {
      messages.value.push({ id: ++msgId, role: 'system', content: `图片 ${file.name} 读取失败` })
    }
  }
}

function removeImage(index: number) {
  attachedImages.value.splice(index, 1)
}

function send() {
  const text = input.value.trim()
  const images = attachedImages.value.map(i => i.dataUri)
  if ((!text && !images.length) || sending.value) return
  if (!ws || ws.readyState !== WebSocket.OPEN || !authed) {
    // 断线状态下发送：乐观显示气泡并排队，连接恢复后自动发出
    pendingSend = text
    pushUser(text || '请描述/分析这张图片', images)
    messages.value.push({ id: ++msgId, role: 'system', content: '正在重新连接…' })
    if (!ws || ws.readyState === WebSocket.CLOSED) connect(currentThreadId)
    return
  }
  pushUser(text || '请描述/分析这张图片', images)
  // 立即创建 AI 占位气泡：发送后马上显示 typing 动画，而非等首个 token 才出现气泡
  ensureAssistant()
  scrollDown()
  ws.send(JSON.stringify({ message: text || '请描述/分析这张图片', images }))
}

function reconnect() {
  connect(currentThreadId)
}

// 线程加载序号：快速连续切换线程时丢弃过期响应，避免旧历史覆盖新历史（竞态）
let threadLoadSeq = 0
const loadingHistory = ref(false)

function selectThread(threadId: string) {
  if (threadId === currentThreadId) return
  // 乐观更新：WS thread_id 事件到达前阻止同一线程被重复加载
  currentThreadId = threadId
  const seq = ++threadLoadSeq
  loadingHistory.value = true
  getThread(threadId).then(data => {
    if (seq !== threadLoadSeq) return  // 过期响应（用户已切到别的线程）
    messages.value = []
    msgId = 0
    let lastAssistant: UIMessage | null = null
    for (const m of data.messages || []) {
      if (m.role === 'user') {
        messages.value.push({ id: ++msgId, role: 'user', content: m.content })
      } else if (m.role === 'assistant') {
        const um: UIMessage = {
          id: ++msgId,
          role: 'assistant',
          content: m.content || '',
          model: m.model,
          renderedHtml: renderMarkdown(m.content || ''),  // 历史消息一次性渲染
          tools: (m.tool_calls || []).map(tc => ({
            name: tc.name, args: tc.args, tag: tagForTool(tc.name), status: 'done' as const,
          })),
        }
        messages.value.push(um)
        lastAssistant = um
      } else if (m.role === 'tool') {
        // 后端契约: { role:'tool', content: 工具名, tool_result: 实际结果 }
        const chip: UIToolChip = {
          name: m.content,
          tag: tagForTool(m.content),
          status: m.error ? 'error' : 'done',
          result: m.tool_result,
        }
        if (lastAssistant) {
          lastAssistant.tools = [...(lastAssistant.tools || []), chip]
        } else {
          messages.value.push({ id: ++msgId, role: 'system', content: `工具 ${m.content}` })
        }
      }
    }
    loadingHistory.value = false
    scrollDown()
  }).catch(() => { if (seq === threadLoadSeq) loadingHistory.value = false })
  connect(threadId)
}

function scrollDown() {
  nextTick(() => {
    const el = document.getElementById('msg-container')
    if (el) el.scrollTop = el.scrollHeight
  })
}

// ── U1：一键复制 AI 回复 ──
const copiedMsgId = ref(0)

async function copyMessage(m: UIMessage) {
  try {
    await navigator.clipboard.writeText(m.content)
    copiedMsgId.value = m.id
    setTimeout(() => { if (copiedMsgId.value === m.id) copiedMsgId.value = 0 }, 1500)
  } catch {
    // 剪贴板 API 不可用（如非 HTTPS 环境），静默降级
  }
}

function handleKeydown(e: KeyboardEvent) {
  // 输入法组合中（拼音选词回车）不触发发送（F6）
  if (e.isComposing || e.keyCode === 229) return
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
}

watch(() => route.params.threadId, (tid) => {
  if (tid && typeof tid === 'string' && tid !== currentThreadId) selectThread(tid)
})

onMounted(async () => {
  const tid = route.params.threadId as string | undefined
  // 深链接直达 /chat/:id 时加载历史（F4）；否则新建连接
  if (tid) selectThread(tid)
  else connect()
  try { const h = await getHealth(); agentReady.value = h.checks?.agent ?? false } catch (_) {}
})

onUnmounted(() => {
  // 先标记 disposed 再关闭，阻止 onclose 里的定时重连（F3）
  disposed = true
  if (ws) { ws.onclose = null; ws.close() }
  ws = null
})
</script>

<template>
  <div class="flex-1 flex flex-col min-w-0 min-h-0 relative">

    <!-- ═══ 顶部状态栏：连接状态 ═══ -->
    <header class="glass h-14 px-6 flex items-center justify-between shrink-0 z-10 border-b border-line">
      <div class="text-[14px] font-semibold text-ink truncate">{{ threadTitle }}</div>
      <div class="flex items-center gap-2 shrink-0">
        <span class="w-2 h-2 rounded-full" :style="{ background: statusColor }" />
        <span class="text-[12px] font-medium text-ink-2">{{ statusText }}</span>
        <button
          v-if="!connected"
          @click="reconnect"
          class="ml-1 text-[12px] font-semibold text-accent px-2 py-0.5 rounded-btn hover:bg-accent-soft transition-colors duration-150">
          重连
        </button>
      </div>
    </header>

    <!-- ═══ 消息区 ═══ -->
    <div id="msg-container" class="flex-1 min-h-0 overflow-y-auto z-10">
      <div class="max-w-[860px] mx-auto px-6 py-8 space-y-5">

        <!-- 历史加载中 -->
        <div v-if="loadingHistory" class="flex flex-col items-center justify-center h-[60vh]">
          <span class="text-[13px] font-medium text-ink-3 animate-pulse">正在加载历史消息…</span>
        </div>

        <!-- 空白初始状态 -->
        <div v-else-if="messages.length === 0 && !sending" class="flex flex-col items-center justify-center h-[60vh]">
          <div class="w-16 h-16 rounded-[18px] flex items-center justify-center mb-6 shadow-card"
               style="background: linear-gradient(135deg, #4A4A4E 0%, #1D1D1F 100%);">
            <AppIcon name="hub" :size="32" :stroke-width="2" class="text-white" />
          </div>
          <h2 class="text-[22px] font-bold tracking-tight text-ink mb-2">Agent Hub</h2>
          <p class="text-[14px] text-ink-2">知识检索 · 联网搜索 · 代码执行 · RPA 自动化</p>
          <div class="flex gap-2.5 mt-7">
            <span class="flex items-center gap-1.5 text-[12px] font-medium px-3.5 py-1.5 rounded-full glass border border-line text-ink-2">
              <AppIcon name="search" :size="12" /> 搜索
            </span>
            <span class="flex items-center gap-1.5 text-[12px] font-medium px-3.5 py-1.5 rounded-full glass border border-line text-ink-2">
              <AppIcon name="code" :size="12" /> 代码
            </span>
            <span class="flex items-center gap-1.5 text-[12px] font-medium px-3.5 py-1.5 rounded-full glass border border-line text-ink-2">
              <AppIcon name="rpa" :size="12" /> RPA
            </span>
          </div>
        </div>

        <template v-for="m in messages" :key="m.id">
          <!-- 系统提示（居中胶囊） -->
          <div v-if="m.role === 'system'" class="flex justify-center msg-in">
            <span class="text-[11px] font-medium px-3.5 py-1.5 rounded-full bg-surface text-ink-2">
              {{ m.content }}
            </span>
          </div>

          <!-- 用户消息（靠右） -->
          <div v-else-if="m.role === 'user'" class="flex justify-end msg-in">
            <div class="max-w-[75%] px-4 py-2.5 rounded-card text-[14px] leading-relaxed whitespace-pre-wrap
                        bg-ink text-white shadow-soft">
              <div v-if="m.images?.length" class="flex flex-wrap gap-2 mb-2">
                <img v-for="(img, i) in m.images" :key="i" :src="img" alt="附件图片"
                     class="max-w-[180px] max-h-[180px] rounded-ctrl object-cover border border-white/20" />
              </div>
              {{ m.content }}
            </div>
          </div>

          <!-- AI 消息卡片（靠左，含工具标签） -->
          <div v-else class="flex justify-start msg-in group">
            <div class="w-7 h-7 rounded-full flex items-center justify-center shrink-0 mt-0.5 shadow-soft"
                 style="background: linear-gradient(135deg, #4A4A4E 0%, #1D1D1F 100%);">
              <AppIcon name="hub" :size="14" :stroke-width="2.4" class="text-white" />
            </div>
            <div class="ml-3 min-w-0 max-w-[85%]">
              <!-- 卡片头：名称 / 模型 / 复制 -->
              <div class="flex items-center gap-2 px-1 mb-1.5">
                <span class="text-[12px] font-semibold text-ink">Agent Hub</span>
                <span v-if="m.model" class="text-[10px] font-medium text-ink-2 px-1.5 py-0.5 rounded-[5px] bg-black/5">
                  {{ m.model }}
                </span>
                <button
                  @click="copyMessage(m)"
                  aria-label="复制回复"
                  class="ml-auto text-[11px] font-medium px-2 py-0.5 rounded-btn text-ink-3 opacity-0
                         group-hover:opacity-100 focus-visible:opacity-100 hover:bg-surface hover:text-ink
                         transition-all duration-150">
                  {{ copiedMsgId === m.id ? '已复制 ✓' : '复制' }}
                </button>
              </div>

              <!-- 消息卡片（半透明玻璃） -->
              <div class="glass-card rounded-card shadow-card px-5 py-4">
                <!-- 执行计划 -->
                <div v-if="m.plan"
                     class="mb-3 rounded-ctrl bg-surface border border-line px-3.5 py-2.5 text-[12px] text-ink-2">
                  <div class="text-[11px] font-semibold text-ink mb-1">执行计划</div>
                  <div class="whitespace-pre-wrap leading-relaxed">{{ m.plan }}</div>
                </div>
                <div class="markdown-body text-[14px] leading-relaxed text-ink" v-html="m.renderedHtml || ''" />
                <!-- 等待首个 token -->
                <div v-if="sending && currentAssistant?.id === m.id && !m.content" class="flex gap-1 pt-1">
                  <span class="typing-dot" />
                  <span class="typing-dot" />
                  <span class="typing-dot" />
                </div>
              </div>

              <!-- 工具标签：搜索 / 代码 / RPA -->
              <div v-if="m.tools?.length" class="mt-2 space-y-1.5">
                <div v-for="(chip, i) in m.tools" :key="i">
                  <button
                    @click="chip.expanded = !chip.expanded"
                    class="w-full flex items-center gap-2 px-3 py-2 rounded-btn border border-line bg-white/40
                           hover:bg-white/70 text-[12px] transition-colors duration-150">
                    <span class="flex items-center gap-1 px-1.5 py-0.5 rounded-[6px] font-medium shrink-0"
                          :class="tagClasses(chip.tag)">
                      <AppIcon v-if="chip.tag" :name="chip.tag.key" :size="11" />
                      {{ chip.tag?.label || '工具' }}
                    </span>
                    <span class="text-ink-2 font-medium truncate">{{ chip.name }}</span>
                    <span class="ml-auto shrink-0 flex items-center gap-1.5">
                      <span v-if="chip.status === 'running'" class="flex items-center gap-1 text-warn">
                        <span class="w-1.5 h-1.5 rounded-full bg-warn animate-pulse" />
                        执行中
                      </span>
                      <AppIcon v-else-if="chip.status === 'done'" name="check" :size="11" class="text-ok" />
                      <AppIcon v-else-if="chip.status === 'error'" name="x" :size="11" class="text-danger" />
                      <AppIcon v-else name="alert" :size="11" class="text-warn" />
                    </span>
                    <AppIcon name="chevron-down" :size="12" class="text-ink-3 shrink-0 transition-transform duration-200"
                             :class="chip.expanded ? 'rotate-180' : ''" />
                  </button>
                  <!-- 展开：参数 + 结果 -->
                  <div v-if="chip.expanded"
                       class="mt-1.5 ml-1 pl-3 border-l-2 border-line text-[12px] text-ink-2 whitespace-pre-wrap
                              break-words leading-relaxed max-h-56 overflow-y-auto">
                    <div v-if="chip.args" class="text-ink-3 mb-1">参数：{{ chip.args }}</div>
                    <div v-if="chip.result">{{ chip.result }}</div>
                    <div v-if="chip.truncated" class="text-ink-3 mt-1 text-[11px]">（结果已截断，仅显示前 300 字符）</div>
                    <div v-if="!chip.args && !chip.result && !chip.logs?.length" class="text-ink-3">（无内容）</div>
                  </div>
                  <!-- RPA 执行日志：实时追加，等宽字体，不设高度上限（页面随日志滚动） -->
                  <div v-if="chip.logs?.length"
                       class="mt-1.5 ml-1 pl-3 border-l-2 border-line">
                    <div class="mb-1 text-[10px] uppercase tracking-wider text-ink-3 font-medium">执行日志</div>
                    <div v-for="(l, j) in chip.logs" :key="j"
                         class="font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words"
                         :class="logLineClass(l.level)">
                      <span v-if="l.time" class="text-ink-3">[{{ l.time }}]</span>{{ l.content }}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </template>

      </div>
    </div>

    <!-- ═══ 底部输入区（悬浮毛玻璃）═══ -->
    <div class="shrink-0 px-6 pb-5 pt-1 z-10">
      <!-- 断线提示 -->
      <div v-if="!connected" class="max-w-[860px] mx-auto mb-2">
        <div class="flex items-center gap-2 rounded-ctrl bg-[#FFF4E5] text-[#C77E1B] px-3.5 py-2 text-[12px] font-medium">
          <AppIcon name="alert" :size="13" />
          连接已断开，正在自动重连…
          <button @click="reconnect"
                  class="ml-auto text-[12px] font-semibold hover:underline underline-offset-2">
            重试连接
          </button>
        </div>
      </div>

      <div class="max-w-[860px] mx-auto">
        <!-- 图片附件预览 -->
        <div v-if="attachedImages.length" class="flex flex-wrap gap-2 mb-2 px-1">
          <div v-for="(img, i) in attachedImages" :key="i" class="relative">
            <img :src="img.dataUri" :alt="img.name"
                 class="w-16 h-16 rounded-ctrl object-cover border border-line shadow-soft" />
            <button @click="removeImage(i)" aria-label="移除图片"
                    class="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-ink text-white text-[12px] leading-none
                           flex items-center justify-center shadow-soft">
              ×
            </button>
          </div>
        </div>

        <div class="glass-strong rounded-card border border-line shadow-card p-2 flex items-end gap-2
                    transition-all duration-200 focus-within:border-ink/15 focus-within:shadow-float">
          <input ref="fileInput" type="file" accept="image/*" multiple class="hidden"
                 @change="onAttachImages(($event.target as HTMLInputElement).files)" />
          <button @click="fileInput?.click()" aria-label="添加图片" :disabled="sending"
                  class="w-10 h-10 rounded-full flex items-center justify-center shrink-0 mb-0.5
                         scale-press transition-all duration-150 text-ink-2 hover:bg-surface disabled:opacity-50">
            <AppIcon name="image" :size="18" />
          </button>
          <textarea
            v-model="input"
            @keydown="handleKeydown"
            :disabled="sending"
            rows="1"
            class="flex-1 bg-transparent resize-none outline-none text-[14px] leading-relaxed px-3 py-2
                   max-h-40 placeholder:text-ink-3 disabled:opacity-50"
            :placeholder="connected ? '输入消息… (Enter 发送，Shift+Enter 换行)' : '连接已断开，重连后自动发送…'"
            @input="(e: Event) => {
              const t = e.target as HTMLTextAreaElement
              t.style.height = 'auto'
              t.style.height = Math.min(t.scrollHeight, 160) + 'px'
            }"
          />
          <button
            @click="send"
            aria-label="发送消息"
            :disabled="sending || (!input.trim() && !attachedImages.length) || !connected"
            class="w-10 h-10 rounded-full flex items-center justify-center shrink-0 mb-0.5 mr-0.5
                   scale-press transition-all duration-150
                   bg-accent hover:bg-accent-hover text-white shadow-soft
                   disabled:bg-black/10 disabled:text-ink-3 disabled:shadow-none"
          >
            <AppIcon name="send" :size="16" class="-translate-x-px translate-y-px" />
          </button>
        </div>
        <p class="text-center text-[11px] text-ink-3 mt-2">Agent Hub 可能会出错，请核对重要信息</p>
      </div>
    </div>

    <!-- ═══ 审批对话框（Human-in-the-Loop）═══ -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showApprovalDialog && pendingApproval" class="fixed inset-0 z-50 flex items-center justify-center">
          <!-- 遮罩 -->
          <div class="absolute inset-0 bg-black/30 backdrop-blur-[2px]" @click="denyAction" />
          <!-- 对话框 -->
          <div class="relative glass-strong rounded-card shadow-float max-w-lg w-full mx-4 p-6 z-10 border border-line">
            <!-- 标题 -->
            <div class="flex items-center gap-3 mb-4">
              <span class="w-9 h-9 rounded-btn bg-[#FFF4E5] text-[#C77E1B] flex items-center justify-center shrink-0">
                <AppIcon name="alert" :size="17" />
              </span>
              <div>
                <h3 class="text-[15px] font-bold text-ink">需要确认操作</h3>
                <p class="text-[12px] text-ink-3 mt-0.5">以下操作涉及高风险，请确认是否继续</p>
              </div>
            </div>

            <!-- 风险操作列表 -->
            <div class="space-y-2.5 mb-5 max-h-64 overflow-y-auto">
              <div v-for="c in pendingApproval.calls" :key="c.id"
                   class="p-3 rounded-ctrl text-[13px]"
                   :class="c.risk_level === 'high'
                     ? 'bg-danger/5 border border-danger/15'
                     : 'bg-[#FFF4E5] border border-[#C77E1B]/15'">
                <div class="flex items-center gap-2 mb-1">
                  <span class="px-1.5 py-0.5 rounded-[5px] text-[10px] font-bold text-white"
                        :style="{ background: c.risk_level === 'high' ? '#FF3B30' : '#FF9F0A' }">
                    {{ c.risk_level === 'high' ? '高风险' : '中风险' }}
                  </span>
                  <code class="text-[12px] font-semibold text-ink truncate">{{ c.name }}</code>
                </div>
                <p class="text-[11px] text-ink-2 leading-relaxed">{{ c.reason }}</p>
              </div>
            </div>

            <!-- 按钮 -->
            <div class="flex gap-3">
              <button @click="denyAction" :disabled="approvalProcessing"
                      class="flex-1 py-2.5 rounded-btn text-[13px] font-semibold transition-colors duration-150
                             disabled:opacity-40 bg-black/5 text-ink-2 hover:bg-black/10">
                拒绝
              </button>
              <button @click="approveAction" :disabled="approvalProcessing"
                      class="flex-1 py-2.5 rounded-btn text-[13px] font-semibold text-white transition-colors
                             duration-150 disabled:opacity-40 bg-accent hover:bg-accent-hover">
                批准
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
/* 模态过渡（克制） */
.modal-enter-active, .modal-leave-active { transition: opacity 0.2s ease; }
.modal-enter-from, .modal-leave-to { opacity: 0; }
</style>
