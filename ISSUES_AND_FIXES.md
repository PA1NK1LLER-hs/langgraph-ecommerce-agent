# 问题清单与修复记录

> 生成时间：2026-08-13。审计范围：`src/`（后端）与 `frontend/src/`（前端）全部源码，以及以普通用户视角走查的 UX 问题。
> 状态图例：`[ ]` 待修复 · `[x]` 已修复（附验证方式）。

---

## 一、前端问题

### P0 — 真实 Bug（跨层契约错误，必修）

| ID | 问题 | 证据 | 状态 |
|----|------|------|------|
| F1 | 健康检查字段契约错误：`getHealth` 期望 `agent_ready`，后端实际返回 `checks.agent`，顶部状态灯永远显示"连接中"，永不显示"在线" | `frontend/src/api/client.ts:42` ↔ `src/api/server.py:233`；`ChatView.vue:219` | [x] `getHealth` 类型对齐 `checks`，`agentReady` 读 `checks.agent`；`vue-tsc` 构建通过 |
| F2 | `tool_result` 事件字段错位：前端用 `d.tool`（工具名）当作消息内容渲染，丢弃后端发送的 `d.content`（实际结果） | 后端 `src/api/routers/chat.py:165-171` ↔ `ChatView.vue:109-111` | [x] `tool_result` 渲染 `d.content`、`tool_call` 渲染 `d.args`；构建通过 |
| F3 | 登出/组件卸载后僵尸 WebSocket：`onclose` 无条件 3 秒重连，`onUnmounted` 关闭时不置空 handler，卸载后仍以匿名身份重连 | `ChatView.vue:87, 222` | [x] `disposed` 标志 + `onUnmounted` 置空 `onclose` 再关闭；构建通过 |
| F4 | 断线重连清空消息且不重载历史：`connect()` 无条件 `messages=[]`，网络抖动后当前对话显示内容丢失；且深链接直接进入 `/chat/:id` 时不加载历史 | `ChatView.vue:73-77, 215-217` | [x] 重连不再清空消息；`selectThread` 加载历史并预渲染；构建通过 |
| F5 | 切换线程双重触发：Sidebar 同时 emit + `router.push`，ChatView 又 watch route 变化，导致 `getThread` 与 `connect` 各执行两次 | `Sidebar.vue:53-57` + `ChatView.vue:211-213` | [x] Sidebar 移除 `selectThread` emit，切换线程唯一路径走 router；构建通过 |
| F6 | 中文输入法回车误发送：`handleKeydown` 未检查 `isComposing`，拼音选词回车直接发送消息 | `ChatView.vue:207-209` | [x] 检查 `e.isComposing \|\| e.keyCode === 229`；构建通过 |

### P1 — 架构性设计问题

| ID | 问题 | 证据 | 状态 |
|----|------|------|------|
| F7 | Pinia chat store 是死代码：`stores/chat.ts` 126 行全项目无人 import；ChatView 本地状态与其重复且 `UIMessage` 定义不一致（半成品重构） | `frontend/src/stores/chat.ts`（无引用）；`ChatView.vue:46-52` | [x] 删除 `stores/chat.ts`；构建通过 |
| F8 | WS 类型契约形同虚设：`JSON.parse` 结果为 `any`，所有事件无类型收窄；`structured_output` 事件后端会发、类型已声明，但前端无处理分支静默丢弃 | `ChatView.vue:91-141`；`types/chat.ts:18,35` | [x] `parseWSEvent` 运行时字段校验 + 联合类型收窄；补 `structured_output` 分支；构建通过 |
| F9 | 无 401 统一处理：token 过期后所有 API 静默失败（`catch(_){}`），WS 以匿名身份继续连接，用户身份悄然丢失且无任何提示 | `client.ts:10-19`；`Sidebar.vue:32,36,40,42` | [x] `client.ts` 统一 401 → 清 token + 跳登录并提示"登录已过期"；构建通过 |
| F10 | XSS 用正则 sanitize 可被绕过（实体编码、`<svg onload>` 等），然后 `v-html` 注入；应使用 DOMPurify | `ChatView.vue:16-28, 340` | [x] `marked.parse` 后经 `DOMPurify.sanitize`；新增 `dompurify@3.4.13` 依赖；构建通过 |
| F11 | JWT 放在 WS URL query string，会泄漏进服务器/代理日志 | `ChatView.vue:81` | [x] URL 不再携带 token；连接后首条消息 `{"type":"auth"}` 认证（后端保留旧 query 兼容）；冒烟测试实测 `auth_ok` 流程通过 |
| F12 | token 双来源：`client.ts` 直接读 localStorage，绕过 Pinia auth store，logout 后可能残留 | `client.ts:5` ↔ `stores/auth.ts:6` | [x] `client.ts` 集中 `getToken/setToken/clearToken`，auth store 复用；构建通过 |

### P2 — 性能与交互问题

| ID | 问题 | 证据 | 状态 |
|----|------|------|------|
| F13 | 流式输出 O(n²)：模板中 `v-html="renderMarkdown(m.content)"` 无缓存，每个 token 到达时所有历史消息的 markdown 都重新 `marked.parse` 一遍 | `ChatView.vue:340` | [x] 结果缓存到 `m.renderedHtml` + 100ms 节流渲染 + done 时 flush；构建通过 |
| F14 | KB 上传进度轮询是死代码：后端 upload 同步索引完才返回，`await` 结束即 100%，2 秒轮询 timer 全程空转 | `KBManagement.vue:75-93, 147-154, 177`；后端 `knowledge.py:227-234` | [x] 删除轮询 timer/进度状态，改为进行中 spinner + 结果提示；构建通过 |
| F17 | `/kb` 页面无导航：没有 Sidebar、没有返回按钮，只能浏览器后退 | `KBManagement.vue:182-206` | [x] 页头新增"返回对话"router-link；构建通过 |
| F18 | `setTimeout(500ms)` 魔法延迟等 Sidebar ref 挂载；Vue 3 中父组件 `onMounted` 时子组件 ref 已可用，延迟不必要 | `ChatView.vue:218` | [x] 移除延迟，`onMounted` 直接调用 `loadThreads()`；构建通过 |
| F19 | 可访问性：删除按钮仅 hover 可见（`v-show` + `group-hover:opacity`），键盘用户不可达；icon-only 按钮无 `aria-label` | `Sidebar.vue:188`；`KBManagement.vue:337` | [x] `v-show` 改为 opacity 切换 + `focus-visible:opacity-100`；删除/登出/发送按钮补 `aria-label`；构建通过 |

### P3 — 维护性与一致性问题

| ID | 问题 | 证据 | 状态 |
|----|------|------|------|
| F15 | 设计 token 定义了但不用：`style.css` 定义 `--accent` 等 token，组件里 90% 硬编码十六进制；根目录 `frontend/style.css` 是旧版残留无人引用；登录/注册用 `#007AFF`，聊天页用 `#4F6EF7`，两套配色 | `src/style.css:6-25`；`frontend/style.css` | [ ] 低优先，待统一设计 token |
| F16 | 死代码：`formatSize` 定义未使用；`reindexSource`/`getApprovalStatus`/`decideApproval` API 绑定未调用；`approvalProcessing` 同步置位从未真正渲染 | `KBManagement.vue:157`；`client.ts:56-57,89`；`ChatView.vue:70,151,161` | [ ] 待清理 |
| F20 | 工程化缺失：无 ESLint/Prettier/单测；登录注册表单重复未抽组件；`BASE=''` 硬编码 | `package.json`；`client.ts:1` | [ ] 待接入 |

---

## 二、后端问题（架构规范维度）

| ID | 问题 | 证据 | 状态 |
|----|------|------|------|
| B1 | **无 service 层**：WS 端点内置审批/恢复/中断协议适配等完整业务流；上传→解析→索引→清理全在路由 | `routers/chat.py`（336 行）；`routers/knowledge.py:154-239, 375-425` | [ ] 大重构，单独排期 |
| B2 | 配置散落 + 导入期崩溃：`config.py` 裸 `os.getenv`；`deps.py` 模块导入期缺 `JWT_SECRET_KEY` 直接 `raise`，整个应用（含 health）无法导入 | `src/config.py`；`api/deps.py:15-20` | [x] 缺密钥改为警告 + 临时随机密钥，应用可启动（重启后 token 失效，生产须显式配置）；实测服务正常启动、health 全绿 |
| B3 | 无统一异常处理器，三种错误风格并存，异常字符串泄漏给客户端 | 全项目无 `@app.exception_handler`；`approval.py:169`、`chat.py:327` | [x] 统一三个 handler：HTTPException / 422 校验（简化，不泄漏字段细节）/ 兜底 500（堆栈只进日志）；实测 422、401、403、429 均为 `{detail, request_id}` 统一结构 |
| B4 | `auth/permissions.py` 反向依赖 Web 层（`try: from api.deps import ...` ImportError hack），失败时权限依赖静默退化 | `auth/permissions.py:159-167, 181` | [x] FastAPI 依赖 `require_admin/require_editor` 移入 `api/deps.py`；permissions 只留纯函数；`auth/__init__.py` 兼容再导出；实测 viewer 上传 KB → 403"需要编辑者以上权限" |
| B5 | 同步阻塞调用未入线程池：reindex 直接同步 `parse_file`（同文件另两处都用了 `to_thread`） | `routers/knowledge.py:392` | [x] reindex 改 `asyncio.to_thread(parse_file, ...)`；编译通过、服务运行正常 |
| B6 | `_agent_cache` 无锁：并发 miss 时重复 build agent；`get_agent` 无并发保护 | `agent/graph.py:1501-1524` | [x] 加 `asyncio.Lock` 保护 get/rebuild（锁内二次检查 + 驱逐）；编译通过、服务运行正常 |
| B7 | IP 提取策略不一致（安全）：`auth.py` 直接信任 `X-Forwarded-For`（可伪造绕过认证限流），`rate_limit.py` 用另一套正确策略 | `routers/auth.py:17-20` ↔ `rate_limit.py:205-224` | [x] auth 路由复用 `rate_limit.get_client_ip`；实测登录限流 429 生效 |
| B8 | 入口混乱：`run.py`（monkeypatch 标准库 `asyncio.timeouts.Timeout`）、`run_server.py`、`server.py:main()`、`main.py` 四入口；端口 8080 硬编码三处 | `run.py:44-47`；`server.py:376` | [ ] 待收敛 |
| B9 | 路由层侵入 agent 私有 API：直接访问 `agent.checkpointer`、手动 `anext(get_db())` 绕过依赖注入 | `threads.py:164-170`；`chat.py:186-187` | [ ] 待重构 |
| B10 | 双份全局 agent 状态：`_agent_cache` 与 `AppContext._agent` 并存，各路由各走一套 | `graph.py:1500`；`app_context.py:112` | [x] 已通过 `_sync_app_context` 同步 + 新增 `get_checkpoint_agent()` 统一入口（历史/审批路由全部走 `_agent_cache`）；完全移除 `AppContext._agent` 的收敛随 B1 重构 |
| B11 | 运行时数据目录污染源码树：`src/api/src/data/lightrag/`、`src/src/data/lightrag/` 嵌套目录 | `git status` | [ ] 待清理迁移 |
| B12 | `graph.py` 巨型函数：`build_agent` 约 1140 行，15+ 闭包节点 | `agent/graph.py:340-1478` | [ ] 大重构，单独排期 |

> 偏离但合理的部分（不算违规）：LangGraph 闭包式节点写法、WS 协议适配放路由、checkpoint 由 agent 管理 —— 属 agent 项目领域惯例。

---

## 三、用户视角 UX 问题（走查新增）

| ID | 问题 | 说明 | 状态 |
|----|------|------|------|
| U1 | 助手消息无复制按钮 | 用户无法一键复制 AI 回复，只能手选 | [x] 助手气泡新增复制按钮（复制成功短暂反馈）；构建通过 |
| U2 | 生成中无法停止 | `sending` 期间输入框禁用，但没有"停止生成"按钮，长任务只能干等（需后端配合取消机制） | [ ] 需后端支持 |
| U3 | 审批对话框无倒计时 | 后端 300 秒超时自动拒绝，前端对话框无提示，超时后点"批准"实际无效且无反馈 | [ ] 需后端下发 TTL |
| U4 | 删除文档用原生 `confirm()/alert()` | 与整体 UI 风格割裂；错误信息直接 alert | [x] 两步确认按钮（3 秒自动取消）+ 行内错误提示，替代原生弹窗；构建通过 |
| U5 | 移动端布局挤压 | Sidebar 固定 `w-60` 无响应式收起，手机上聊天区被压没 | [ ] 待响应式改造 |
| U6 | 断线重连无感知 | 重连过程中输入的消息会丢失（发出去但连接已断），无提示 | [x] 部分缓解：重连不清空消息、断线时 placeholder 提示"正在重连"（随 F4）；连接态图标提示待补 |
| U7 | 登录后总是跳 `/chat` 新建对话 | 不回到用户离开前的位置；token 过期后无感退化为匿名会话 | [x] 部分缓解：401 统一跳登录并提示"登录已过期"（随 F9）；"回到离开前位置"待做 |
| U8 | 线程列表无分页/搜索 | 对话多了以后 Sidebar 无限增长，找不到历史对话 | [ ] 需后端分页 |
| U9 | 无暗色模式 | 全局硬编码浅色值，扩展成本高（与 F15 同根因） | [ ] |
| U10 | 无密码修改/个人中心入口 | 注册后无法改密码 | [ ] 功能缺失 |

---

## 四、启动期修复（已完成）

| ID | 问题 | 修复 | 状态 |
|----|------|------|------|
| S1 | `langgraph-checkpoint-sqlite` 缺失，checkpoint 回退 MemorySaver（重启丢会话） | 安装依赖并同步 `requirements.txt` / `pyproject.toml` | [x] 日志确认 `SQLite checkpoint store ready`，`/api/health` 全绿 |

---

## 五、修复顺序与验证方式

1. 前端 P0（F1-F6）→ `npm run build`（vue-tsc 类型检查）+ 手工走查 ✅
2. 前端 P1（F7-F12）→ 同上 ✅
3. 前端 P2 + UX（F13-F19、U1、U4、U6、U7）→ 同上 ✅
4. 后端（B2-B7）→ 重启 + `/api/health` + WS 端到端冒烟（`scripts/ws_smoke_test.py`）✅
5. 全链路回归 + 更新本文档状态 ✅（见下方验证记录）
6. 大重构项（B1 service 层、B12 巨型函数、U2/U3/U8 后端配合）单独排期

---

## 六、全链路回归验证记录（2026-08-13）

| # | 验证项 | 结果 |
|---|--------|------|
| 1 | 后端重启，启动日志 | ✅ MCP 14+2 工具加载、数据库初始化、SQLite checkpoint ready、Agent ready |
| 2 | `/api/health` | ✅ `{"status":"healthy"}` 四项检查全绿 |
| 3 | REST：登录 / me / threads / kb stats | ✅ 全部 HTTP 200 |
| 4 | WS 新认证流程（URL 无 token，首条消息 auth） | ✅ 收到 `thread_id` + `auth_ok: true`，聊天消息全流式返回（30 事件，LLM 回复完整） |
| 5 | 统一异常结构（B3） | ✅ 422 → `{"detail":"请求参数有误"}`；401/403 → 人话 detail + `request_id` |
| 6 | 认证限流（B7） | ✅ 登录超频 429 + Retry-After |
| 7 | 角色权限（B4） | ✅ viewer 上传 KB → 403"需要编辑者以上权限" |
| 8 | 前端构建产物 | ✅ `vue-tsc` + `vite build` 通过，`index-Cmkzf7kS.js` 正常服务 |
| 9 | 冒烟测试稳定性 | ✅ 预热后连续通过；冷启动首次认证（mem0 上下文加载）偶发慢于 15s，属已知冷路径延迟，非协议错误 |

> 备注：冒烟测试脚本已同步为新协议（`scripts/ws_smoke_test.py`，URL 不带 token）。

---

## 七、历史对话修复专项（2026-08-13，第二轮）

### 用户现象

点击侧边栏历史对话 → 聊天框不切换 / 切换后不显示历史消息。

### 根因（三个独立 Bug 叠加）

| # | 根因 | 证据 | 修复 |
|---|------|------|------|
| H1 | **双 agent 状态脱节**：`graph._agent_cache` 与 `AppContext._agent` 并存，`AppContext._agent` 从未被赋值。`threads.py` 读历史走 `AppContext` → 永远拿到 `None` → `messages=[]` | checkpoint 库中历史消息完好，但 `GET /api/threads/{id}` 恒返回空 | `get_checkpoint_agent()` 统一入口（复用 `_agent_cache`，共享同一 SQLite checkpoint）+ `_sync_app_context()` 同步双状态；`threads.py`/`approval.py` 全部改走新入口 |
| H2 | **断连取消导致回复不进 checkpoint**：客户端刷新/切换线程/断开 WS → `astream` 生成器被 aclose → 正在执行的 agent 节点收到 `CancelledError` → 图以 `__error__` 结束 → AIMessage 从未写入 checkpoint → 历史只剩用户消息 | checkpoint 库中 4 个线程的 writes 表存在 `channel='__error__'` 且值为 `'CancelledError()'`，其 agent 节点无任何输出写入；健康线程（be41d57b）无此记录且历史完整 | `chat.py`：所有 WS 发送改 `_safe_send`（发送失败不中断流消费）；流执行放入 `asyncio.create_task` + `asyncio.shield`，handler 被取消时后台任务继续跑完，保证 checkpoint 落盘 |
| H3 | **前端切换竞态**：快速连点两个线程时旧线程的 `getThread` 响应晚到，覆盖新线程消息；`connect()` 后旧 assistant 引用残留 | `ChatView.vue` 无请求序号、无加载态 | `threadLoadSeq` 过期响应丢弃 + 乐观更新 `currentThreadId` + `loadingHistory` 状态 + `currentAssistant` 重置 + `tool_result` 显示真实结果 |

### 验证

| # | 验证项 | 结果 |
|---|--------|------|
| 1 | 正常流程：登录 → 建线程 → WS 聊天 → done → `GET /api/threads/{id}` | ✅ 历史含 `['user','assistant']`，助手回复完整 |
| 2 | 断连流程：收到首个 token 即断开 WS（模拟切换线程/刷新）→ 后台图继续跑完 | ✅ 断连后轮询历史，助手回复仍写入 checkpoint |
| 3 | 双状态回归：`/api/approvals/{id}`、删除线程清理均走共享 agent | ✅ 编译 + 运行正常 |

> 验证脚本：`scripts/verify_history_fix.py`（两个场景独立线程，可重复运行）。

---

## 八、剩余用户体验问题分析（前后端，2026-08-13 第二轮走查）

### P1 — 严重影响

| ID | 问题 | 证据 / 说明 | 建议 |
|----|------|------|------|
| UX-A1 | **单个请求可拖死整个服务（"豆袋船"卡死断联）**：查询"帮我查一下有关豆袋船的信息"时后端事件循环整体阻塞（`/api/health` 超时、CPU 空闲），需重启恢复。**根因已定位并修复（py-spy 实锤）**：`semantic_cache._compute_embedding` 旧实现把 `asyncio.run(ef.func(...))` 丢进 `ThreadPoolExecutor` 再从主循环 `future.result()` 等待——embedding 客户端是共享的 AsyncOpenAI 单例，连接池绑定主事件循环，worker 线程里的 `asyncio.run` 跨循环等待同一连接池，与主循环 join 互相死锁；外层 timeout 失效后 `pool.__exit__ → shutdown(wait=True) → join()` 把主事件循环永久卡死。**修复**：① `_compute_embedding` 改为主循环直接 `await` + `asyncio.wait_for(timeout=10)`；② `rag/embedding.py` 客户端加 `timeout=30, max_retries=1`；③ `client_factory.py` 统一加 `timeout=120, max_retries=2`。`scripts/repro_doudaichuan.py` 复现验证：修复前必现卡死，修复后 7.2s 正常 done（知识库命中 qiaodoudou 产品，流式回答完整）。附带修复：`tool_index_knowledge` 的 `parse_file`/`parse_url`（同步 I/O）改 `asyncio.to_thread` | 已修复。遗留：① 对全部 async 工具做一轮 sync-I/O 审计；② 给 WebSocket 聊天流程加整体超时 + 慢任务日志，防同类问题复发 |
| UX-A2 | **孤儿 tool_calls 迭代风暴**：挂起期间日志反复打印 `Dropping orphaned tool_calls`（同一批 2 个 ID）——`reflect` 中断后 AIMessage 的 tool_calls 残留在 state 中，`_sanitize` 只在发 API 前临时剔除，state 本身未修复 → agent 反复重试工具，烧 token、拖长响应（仅 `MAX_ITERATIONS=30` 兜底） | 在 reflect/plan_check 路由前真正修复 state（移除孤儿 tool_calls 或补 ToolMessage），而不是只做 API 层 sanitize |

### P2 — 明显影响体验

| ID | 问题 | 证据 / 说明 | 建议 |
|----|------|------|------|
| UX-A3 | 冷启动首次认证慢：首次 WS 认证路径触发 mem0 上下文加载 + PostHog 配额检查，实测可达 15s+，期间前端无任何提示 | 冒烟测试冷启动超时记录；日志中 posthog quota 警告 | ① 前端在 `auth_ok` 前显示"正在初始化会话…"；② 后端启动时预热 mem0；③ 无 telemetry 需求时提供环境变量关闭 PostHog |
| UX-A4 | PostHog 配额告警刷日志（每次冷启动 + 认证路径），既噪音又增加延迟 | 日志 `[FEATURE FLAGS] Quota limit exceeded` 反复出现 | 同上 ③；或在配置文件显式禁用 |
| UX-A5 | 登录限流 10 次/分钟/IP 对开发环境偏紧：连续输错几次密码即 429 锁 1 分钟，前端登录页未展示 Retry-After 信息 | B7 实测 429 生效；`LoginView.vue` 未处理 429 | 登录页解析 429 detail 展示"尝试次数过多，请 X 秒后重试"；开发环境可放宽 |
| UX-A6 | 断连后任务仍在后台烧 token（H2 修复的代价）：用户刷新页面以为"取消了"，实际生成继续且费用照计；界面无任何"该对话仍在生成"提示 | 断连流程实测：断开后图继续跑完 | ① 前端历史列表标记"生成中"状态（checkpoint 存在 pending 时）；② 中/长期配合 U2 做真正的取消机制 |
| UX-A7 | 生成中无法停止（U2）、审批无倒计时（U3）：与 UX-A6 同源，需要"用户主动取消"的后端协议 | `sending` 期间输入禁用但无停止按钮 | 设计 `{"type":"cancel"}` WS 消息 → 取消当前 run_task（注意：取消后需保留已 checkpoint 的部分） |

### P3 — 体验割裂 / 细节

| ID | 问题 | 证据 / 说明 | 建议 |
|----|------|------|------|
| UX-A8 | 匿名会话无法转正：未登录聊天也建线程（user_id=0），登录后这些对话不出现在列表，用户以为历史丢了 | `chat.py` `ensure_thread(0, ...)`；`threads.py` 按 `user_id` 过滤 | 登录成功后将匿名线程归属迁移到当前用户（前端传匿名 thread_id）；或明确提示 |
| UX-A9 | 历史消息里工具结果显示被后端截断（300 字），长结果看不到全貌、无展开入口 | `chat.py` `str(content)[:300]`；`threads.py` `tool_result` 同样截断 | 前端 tool 气泡加"展开全文"；截断点加省略标记 |
| UX-A10 | 历史消息不区分"生成中被打断"的对话：H2 修复前产生的坏线程（只有用户消息）仍会显示为空对话 | checkpoint 中 4 个线程含 `__error__` 历史 | 读取历史时检测 `__error__`，前端显示"该对话在生成中被中断"并允许删除 |
| UX-A11 | 既有待办：U5 移动端、U8 分页/搜索、U9 暗色、U10 改密码、F15 设计 token、F16 死代码、F20 工程化 | 见第三/一节 | 按原计划排期 |

### 本轮代码变更清单

| 文件 | 变更 |
|------|------|
| `src/api/routers/chat.py` | `_safe_send`（发送失败不中断流）；`_run_turn`（流消费独立化）；`asyncio.shield` 保护后台落盘；审批等待超时发送安全化 |
| `src/agent/core.py` | `tool_index_knowledge` 的 `parse_file`/`parse_url` 改 `asyncio.to_thread`（UX-A1 ①） |
| `src/cache/semantic_cache.py` | `_compute_embedding` 改主循环异步 + `wait_for(10s)`（UX-A1 根因修复） |
| `src/rag/embedding.py` | AsyncOpenAI 客户端加 `timeout=30, max_retries=1`（UX-A1 根因修复） |
| `src/agent/client_factory.py` | 统一 `timeout=120, max_retries=2`，上游挂起快速失败（UX-A1 根因修复） |
| `scripts/repro_doudaichuan.py` | 新增："豆袋船"卡死复现脚本（90s 无事件判定卡死） |
| `scripts/verify_history_fix.py` | 新增：正常流程 + 断连流程双场景验证脚本 |

## 九、界面重构：苹果风桌面端工作台（2026-08-13）

按需求将 Vue 前端重构为苹果 Apple 原生浅色工作台（保留 Vue 3 + Vite + Tailwind v4 技术栈）：

- **设计系统**（`style.css`）：Tailwind v4 `@theme` token 注册（`bg-accent`/`rounded-card`/`shadow-card` 等工具类）——纯白底色、低饱和浅灰分割线（#ECECF1）、柔和淡蓝强调色（#0A84FF，移除原紫色渐变）、圆角规范 控件 12 / 卡片 18 / 按钮 10、弥散轻阴影、毛玻璃（`.glass`/`.glass-strong`）、系统字体栈（移除 Google Fonts 外链）；删除 pulse-glow/gradient-text/border-glow 等炫酷动画。
- **壳层**（新增 `layouts/WorkspaceLayout.vue` + `stores/threads.ts`）：左侧 288px 固定侧边栏 + 右侧 router-view；线程列表抽到 Pinia store（ChatView 与 Sidebar 共用）。
- **侧边栏**（`Sidebar.vue` 重写）：① 毛玻璃品牌头部（Logo + Agent Hub）② 用户信息（账号 + 退出）③「+ 新建对话」主按钮 ④ 可滚动历史列表（hover 删除）⑤ 底部导航：知识库管理 / 知识库 / 记忆。
- **聊天视图**（`ChatView.vue` 重写）：顶部状态栏（标题 + 连接状态 + 断线重连，断线时发送自动排队重发）；用户消息靠右浅蓝气泡、AI 消息靠左白卡（Agent Hub 头像 + 模型标签 + 复制）；**AI 卡片支持工具标签**（`utils/tools.ts` 按工具名归类 搜索/代码/RPA/记忆，chip 显示执行中/✓/✗ 状态，点击展开参数与结果）；空白态居中 Agent Hub 图标 + slogan「知识检索 · 联网搜索 · 代码执行 · RPA 自动化」+ 三枚功能标签；底部大圆角毛玻璃悬浮输入区 + 圆形发送键；加载 / 断线 / 空对话三态齐备；审批弹窗苹果化。
- **新增页面**：`KnowledgeView.vue`（知识库语义搜索，hybrid/dense 分段控件）、`MemoriesView.vue`（记忆 CRUD）；`KBManagement.vue` 重画并移入壳层；`LoginView`/`RegisterView` 统一品牌与圆角规范；新增 `AppIcon.vue`（内联 SVG 图标组件，无外部依赖）。
- **验证**：`npm run build`（vue-tsc + vite）通过；`/`、`/kb` 路由与静态资源 200；`scripts/ws_smoke_test.py` 冒烟 PASS；`/api/kb/search`（hybrid）与 `/api/memories` 正常。

## 十、界面二轮优化：输入框布局修复 + 黑白灰玻璃磨砂主题 + 模型名显示（2026-08-13）

用户反馈三个问题，逐一修复：

- **历史消息下输入框消失（布局 bug）**：flex 列链中 `flex-1 overflow-y-auto` 的消息区缺少 `min-h-0`，长历史时按内容高度撑大根节点，输入区被顶出视口（`overflow-hidden` 裁掉）。修复：`WorkspaceLayout` 的 `main`、`ChatView` 根节点与 `#msg-container`、以及 KB/知识库/记忆三个子页的滚动容器全部补 `min-h-0`。
- **黑白灰 + 透明玻璃磨砂主题**（`style.css` 令牌重定义 + 各组件调整）：强调色改为墨黑（`--color-accent: #1D1D1F`，主按钮/激活态），`accent-soft`/`surface`/`line` 全部改为黑色低透明度（5%/4%/8%），蓝色渐变 Logo 全部换墨黑渐变（`#4A4A4E→#1D1D1F`），favicon 单色化；`body` 加极淡单色环境渐变给毛玻璃提供层次；新增 `.glass-card`（半透明白 + 白纱边，内容卡片省略 backdrop-filter 避免长列表 GPU 开销）；侧边栏整栏毛玻璃，用户气泡改墨黑白字，AI 卡片/工具标签/登录注册/子页面卡片全部半透明玻璃化；工具标签单色化（图标区分类型，仅执行状态保留语义色）。
- **模型标签 flash 不是写死**：后端 Flash/Pro 双模型路由（`graph.py` 按意图/长度/消息数路由，简单对话走 Flash 是设计行为），但 API 返回的是内部键 `flash`/`pro` 而非真实模型名。修复：`config.py` 新增 `display_model_name()` 映射到 `.env` 真实模型 ID；`threads.py`（历史）与 `chat.py`（流式）统一映射。另发现流式 token 分块不携带 `additional_kwargs`（模型标记丢失），在 `_send_node_output` 补发独立 `model` 事件，前端 `parseWSEvent` + `ChatView` 据此在流式结束后标注回复卡片。
- **验证**：构建通过；后端重启后 health 200；历史消息 API 返回 `deepseek-v4-flash`；流式 `model` 事件实测携带真实模型名；`scripts/ws_smoke_test.py` 冒烟 PASS。

## 十一、SearXNG 联网搜索全部无结果（2026-08-13）

**现象**：`mcp_searxng_web_search` 返回 `{"status": "success", "results": ["🔍 No results found ..."]}`。

**根因**：SearXNG 容器本身正常（API 响应、DNS、TCP 均通），问题在引擎配置与网络可达性的错配——默认配置启用的通用网页引擎是 **brave / duckduckgo / startpage**，这三个站点在当前网络下全部 `httpx.ConnectTimeout`（容器日志可见，每次搜索白等 3×3s）；唯一可能返回结果的 google 被 consent 页面拦截（解析出 0 条且无报错）；而国内可达的 **baidu** 和 **bing（cn.bing.com）** 在 settings.yml 中默认是 `disabled: true`。结果必然为空。

**修复**（`data/searxng/settings.yml`，docker 卷，改完 `docker restart langgraph-agent-searxng-1` 生效）：
- 启用 `baidu`（实测 10 条结果）与 `bing`（`base_url: https://cn.bing.com`，国内实例直连 cn 端点）。
- 禁用 `brave`/`brave.images`/`brave.videos`/`brave.news`、`duckduckgo`（含 images/videos/news）、`startpage`（含 news/images），消除超时拖慢。

**验证**：实例直查「宁波天气」返回 19 条（baidu+bing，命中 weather.com.cn / nmc.cn 等真实来源），`unresponsive_engines` 为空；端到端 WS 测试确认 `mcp_searxng_web_search` 返回真实搜索结果，Agent 后续 url_read / add_memory 链路正常。

## 十二、双模型路由策略调整 + 运行时模型回退（2026-08-13）

用户反馈两点：① agent 没有长时间编码需求，Flash 已足够，希望约 90% 回答走 Flash；② 创建期三级回退只覆盖实例创建失败，运行时 API 调用失败无回退。

**一、路由策略调整**（`graph.py` call_model 路由块）：

- 旧规则：`need_rpa or intent=="complex" or len(input)>200 or len(messages)>10` → Pro，导致大多数多轮/稍长对话都走 Pro。
- 新规则：**仅两类场景走 Pro**——RPA 任务（高风险自动化，必须）；complex 意图且输入超 `PRO_ROUTE_MIN_CHARS`（默认 1000 字）或会话超 `PRO_ROUTE_MIN_MESSAGES`（默认 30 条）。其余全部 Flash。
- 阈值通过 `config.py` 环境变量可调（`PRO_ROUTE_MIN_CHARS` / `PRO_ROUTE_MIN_MESSAGES`，已补入 `.env.example`）。
- 路由决策新增 INFO 日志（`路由决策: need_rpa=... intent=... user_len=... msg_count=...`），排查路由问题直接 grep。

**二、运行时回退链**（`graph.py`）：

- `build_agent` 中预创建 `LLM_FALLBACK_MODEL` / `LLM_FALLBACK_MODEL_2` 实例（去重 flash/pro 后加入回退链）。
- call_model 的单一 `ainvoke` 改为按序尝试：**路由选定模型 → 另一档模型（flash↔pro）→ LLM_FALLBACK_MODEL → LLM_FALLBACK_MODEL_2**；每级失败记 WARNING 日志，全部失败抛 `RuntimeError`（携带最后错误）。
- 回退成功后：`_model_used` 记录实际模型（前端模型标签如实显示）、`_route_reason` 追加 `(fallback: xxx)`、成本统计用实际模型名、语义缓存写入实际使用的模型 key。
- 工具绑定从「路由时一次性 bind」改为「每次尝试时按 tools_to_bind 单独 bind」（`active_llm` 变量移除）。

**验证**（新增 `scripts/verify_model_routing.py`，支持 `--expect-fallback` 模式）：

- 13 字短查询 → `deepseek-v4-flash` ✓；1196 字 complex 查询 → 6 次 agent 迭代全部 `deepseek-v4-pro`（日志确认）✓。
- 以无效 `LLM_FLASH_MODEL` 启动后端：全新短查询 flash 调用 400 → 日志 `模型 deepseek-v4-flash-invalid 调用失败（回退链 1/2）` + `模型回退生效: ... → deepseek-v4-pro`，model 事件与回复均正常（17×23=391）✓。
- 排查备注：短查询曾「误报 flash」——同文案此前已入语义缓存（精确 hash 命中），缓存回复直接沿用存储的 model_key，并非路由失效；判定路由需用全新文案或看 `路由决策` 日志。

## 十三、知识库更新链路修复（删旧→导新）（2026-08-13）

用户反馈：知识库对应关系变化后无法更新——重复上传同一 source 只做增量合并，旧实体/关系不会被覆盖。

**三处修复**：

1. **DELETE 端点**（`src/api/routers/knowledge.py`）：旧实现 import 不存在的 `_get_rag`（500）、且把 source 名当 doc_id 传给 `adelete_by_doc_id`。重写为调用 `indexer.delete_source_async(source_id)`——按 `file_path == source` 遍历 doc_status 逐个 `adelete_by_doc_id`（LightRAG 级联删除分块与仅属于该文档的实体/关系）。
2. **reindex 端点**（新增）：multipart 上传新文件 + `source` 表单字段；流程 = 先 `delete_source_async(source_name)` 删旧 → 解析 → 逐块重新索引（带进度）。删除/重建机制端到端验证通过。
3. **前端更新按钮**（`KBManagement.vue` + `client.ts reindexSource`）：来源行新增「更新」按钮（两步确认），上传同 source 的新文件触发 reindex。

**验证**（`scripts/verify_kb_update.py`，Neo4j 直查关系边）：

- upload v1（供应商A→产品B）→ 图中 A→B 边存在 ✓
- reindex v2（供应商A→产品C）→ `removed_old=2`（含历史残留）、A→C 边存在、**A→B 边消失** ✓
- DELETE → 实体从图与 stats 中消失 ✓

**排查备注**：

- 不用 `/api/kb/search` 做断言：dense 搜索在 Qdrant 无命中时走 Neo4j 全文兜底，`search_labels` 对中文查询按字面匹配返回无关实体，且 `_search_structured_async` 的 chunk 正则（`\[(\d+)\]\s+...`）会把「## 匹配实体」段的实体 ID 误解析为结果内容。关系验证一律用 `docker exec langgraph-agent-neo4j-1 cypher-shell ...` 直查（实体节点用 `entity_id` 属性，边类型 `DIRECTED`；`--format plain` 输出为 `"值1", "值2"` 逗号分隔，勿按 tab 解析）。
- 测试文本 v2 不能再提及旧实体（如「不再向 产品B 供货」），否则 LLM 仍会抽取 A→B 边导致断言失败。
- 权限：upload/reindex/delete 均要求 editor+，而 users 表全部账号（含名为 admin 的）都是 viewer → **KB 管理页对所有用户 403**（历史遗留，见待办）。

