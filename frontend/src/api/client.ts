const BASE = ''

// ── token 单点管理（F12）：stores/auth.ts 与本模块共用，不再各自直读 localStorage ──
const TOKEN_KEY = 'token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}
export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t)
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

function headers(): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) h['Authorization'] = `Bearer ${token}`
  return h
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const opts: RequestInit = { method, headers: headers() }
  if (body) opts.body = JSON.stringify(body)
  const res = await fetch(`${BASE}${path}`, opts)
  if (res.status === 401) {
    // 令牌过期/无效：清理并回登录页，避免用户以"幽灵身份"继续操作（F9）
    clearToken()
    localStorage.removeItem('username')
    if (location.pathname !== '/login') location.href = '/login'
    throw new Error('登录已过期，请重新登录')
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = Array.isArray(err.detail) ? '请求参数有误' : err.detail
    throw new Error(detail || `HTTP ${res.status}`)
  }
  return res.json()
}

export const login = (username: string, password: string) =>
  request<{ access_token: string; username: string }>('POST', '/api/auth/login', { username, password })

export const register = (username: string, password: string) =>
  request<{ access_token: string; username: string }>('POST', '/api/auth/register', { username, password })

export const getTools = () => request<{ count: number; tools: { name: string; description: string }[] }>('GET', '/api/tools')

export const getKBStats = () => request<{ total_chunks: number; sources: { source: string; chunks: number }[] }>('GET', '/api/kb/stats')

export const searchKB = (query: string, topK = 5, mode: 'dense' | 'hybrid' = 'dense') =>
  request('POST', '/api/kb/search', { query, top_k: topK, mode })

export const getMemories = () => request<{ count: number; results: { memory: string }[] }>('GET', '/api/memories')

export const addMemory = (content: string, category = 'general') =>
  request('POST', '/api/memories', { content, category })

export const deleteMemory = (query: string) =>
  request('DELETE', '/api/memories', { query })

export const getHealth = () => request<{ status: string; checks: { agent: boolean; database: boolean; qdrant: boolean; mem0: boolean } }>('GET', '/api/health')

// Threads
export interface ThreadItem { id: number; thread_id: string; title: string; created_at: string; updated_at: string }
export interface ThreadMessage { role: string; content: string; model?: string; tool_calls?: { name: string; args: string }[]; tool_result?: string; error?: boolean }

export const listThreads = () => request<{ threads: ThreadItem[]; count: number }>('GET', '/api/threads')
export const createThread = (title?: string) => request<ThreadItem>('POST', '/api/threads', { title: title || '新对话' })
export const getThread = (threadId: string) => request<{ thread_id: string; title: string; messages: ThreadMessage[] }>('GET', `/api/threads/${threadId}`)
export const deleteThread = (threadId: string) => request<void>('DELETE', `/api/threads/${threadId}`)
export const updateThreadTitle = (threadId: string, title: string) => request<ThreadItem>('PATCH', `/api/threads/${threadId}`, { title })

// Approval
export interface ApprovalStatus { thread_id: string; has_pending: boolean; pending_calls: { id: string; name: string; args: Record<string, unknown>; risk_level: string; reason: string }[] }
export const getApprovalStatus = (threadId: string) => request<ApprovalStatus>('GET', `/api/approvals/${threadId}`)
export const decideApproval = (threadId: string, decision: 'approve' | 'deny') => request<{ status: string; decision: string }>('POST', `/api/approvals/${threadId}`, { decision })

// KB Management
export interface KBSource { source: string; chunks: number; last_indexed?: string }
export interface UploadResult { task_id: string; file: string; source: string; total_chunks: number; indexed_chunks: number }
export interface ImportURLResult { task_id: string; url: string; source: string; total_chunks: number; indexed_chunks: number }
export interface UploadProgress { status: string; file?: string; source?: string; progress: number; total_chunks?: number; indexed?: number; error?: string }

export const uploadFile = (file: File, tags = '', chunkStrategy = 'semantic') => {
  const formData = new FormData()
  formData.append('file', file)
  const params = new URLSearchParams({ tags, chunk_strategy: chunkStrategy })
  const token = getToken()
  return fetch(`${BASE}/api/kb/upload?${params}`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  }).then(r => { if (!r.ok) return r.json().then(e => { throw new Error(e.detail || `HTTP ${r.status}`) }); return r.json() })
}

export const importURL = (url: string, tags = '') =>
  request<ImportURLResult>('POST', '/api/kb/import-url', { url, tags })

export const getUploadProgress = (taskId: string) =>
  request<{ progress: UploadProgress }>('GET', `/api/kb/upload-progress/${taskId}`)

export const getSourceChunks = (sourceId: string, limit = 50) =>
  request<{ source: string; chunks: { content: string; score?: number; source?: string; chunk_id?: string }[]; count: number }>('GET', `/api/kb/sources/${encodeURIComponent(sourceId)}/chunks?limit=${limit}`)

export const deleteSource = (sourceId: string) =>
  request<{ status: string; deleted: string; message?: string }>('DELETE', `/api/kb/sources/${encodeURIComponent(sourceId)}`)

export const reindexSource = (source: string, file: File) => {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('source', source)
  const token = getToken()
  return fetch(`${BASE}/api/kb/reindex`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  }).then(r => { if (!r.ok) return r.json().then(e => { throw new Error(e.detail || `HTTP ${r.status}`) }); return r.json() })
}
