# LangGraph Agent

基于 [LangGraph](https://github.com/langchain-ai/langgraph) 构建的生产级 AI Agent，支持多工具调用、知识库检索、Human-in-the-Loop 审批、多 Agent 协作和 RPA 自动化。前端采用 Vue 3 + TypeScript，后端使用 FastAPI + WebSocket 流式对话。

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend (Vue 3)                         │
│          WebSocket 流式对话 · KB 管理 · 记忆 · 审批交互            │
└──────────────────────────┬──────────────────────────────────────┘
                           │ ws:// /api/*
┌──────────────────────────▼──────────────────────────────────────┐
│                   FastAPI Server (:8080)                         │
│   JWT 认证 · 速率限制 · RBAC · SSRF 防护 · XSS 防护               │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                    LangGraph StateGraph                          │
│                                                                  │
│  classify_intent → supervisor → run_specialist → agent → tools   │
│       │                 │  │            │         │              │
│       ▼                 │  ▼            ▼         ▼              │
│  query_rewrite       planner     子图独立执行    check_approval   │
│       │             (委派后规划) (researcher/   (Human-in-Loop)   │
│       ▼                 │        coder/analyst)                  │
│  search_rag ────────────┴───────────► agent ─────────────────   │
│                                                                  │
│  Flash LLM: 意图分类 / 查询改写 / 摘要                            │
│  Pro LLM:    推理 / 规划 / 工具调用 / 代码生成                      │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                      Infrastructure                              │
│  Qdrant (向量) · Neo4j (图) · PostgreSQL (线程/记忆)             │
│  Redis (限流/缓存) · SQLite (checkpoint) · LightRAG              │
│  Mem0 (记忆) · MCP (Playwright/SearXNG/Filesystem/...)           │
└─────────────────────────────────────────────────────────────────┘
```

## 核心特性

### Agent 能力
- **双模型架构**：Flash 模型处理意图分类、查询改写、对话摘要；Pro 模型处理复杂推理和工具调用（`LLM_FLASH_MODEL` / `LLM_MODEL` 可分别配置）
- **意图路由**：自动识别 8 种意图（闲聊/知识检索/联网搜索/文件操作/代码执行/RPA/时间/复杂任务），跳过不必要的处理节点；RPA 类意图按需惰性挂载 MCP 工具
- **Plan-Execute-Replan**：复杂任务自动生成结构化执行计划，失败后自动重新规划
- **Supervisor 模式（真 Sub-Agent）**：跨领域任务自动委派给专业子 Agent（研究员/代码专家/数据分析师），委派后由父图 `run_specialist` 节点**命令式执行一张独立编译的 LangGraph 子图**（无 checkpointer、串行），子代理在子图内独立跑完自己的思考-工具循环，最终报告合并回主 state 并以 AI 消息流式输出；通用助手（general）保持单循环。子代理内高风险工具（代码执行/文件修改/RPA）**不触发中断**，由子代理在报告中标注待审批操作，主代理以 general 身份经现有审批弹窗执行——审批链路与安全性不变。前端收到 `specialist_started`（运行中）与 `specialist_result`（完成摘要）事件，以工具 chip 展示子代理执行过程
- **Human-in-the-Loop**：高风险操作（代码执行、文件修改）自动触发审批中断，支持 WebSocket 实时审批；审批通过后仍有 RBAC 工具级守卫，代码进入 Docker 沙箱执行
- **命令级执行策略**：`exec_policy.py` 提供前缀规则引擎（借鉴 Codex execpolicy），对 shell 命令做 allow/prompt/forbidden 三级裁决，是工具名审批之外的第二道闸
- **上下文管理**：`context_budget.py` 提供 token 预算估算、摘要去重、噪声/图片剥离，长对话自动压缩为结构化摘要，防止上下文溢出；`tool_get_context_remaining` 元认知工具可查看剩余预算
- **模型回退**：主模型故障时自动切换备用模型（`LLM_FALLBACK_MODEL` / `LLM_FALLBACK_MODEL_2`）

### 知识库
- **混合检索**：BM25 关键词 + Dense 向量 + 知识图谱三路并行检索，RRF 融合排序 + DashScope rerank 精排（`RAG_MODE=hybrid`，默认开启）
- **多格式解析**：PDF / DOCX / XLSX / PPTX / HTML / Markdown / 图片（Vision OCR，`vision.py`）自动解析入库，Excel 支持按行切分索引
- **来源引用**：RAG 回答自动附带 `[来源: 文件名, 相关度: 0.XX]` 格式的精确引用
- **语义缓存**：相似问题复用缓存结果，降低 API 成本（`cache/semantic_cache.py`，KB 变更后自动失效）

### RPA 自动化（`src/skills/`）
- **世贸通抬头报关**（`shimaotong/`）：`excel_path` 留空时自动发现网络共享跟踪表（`SHIMAOTONG_TRACKING_BASE`）中的待办报关 Excel 并批量处理（登录一次、订单号序号跨文件递增）；也可传显式路径做单文件处理。流程：登录（ddddocr 自动识别验证码）→ 检测子订单 → 生成订单号 → 逐个构建保存 → 可选提交报关资料；订单号冲突时自动提取系统新号重试
- **广告花费报表**（`rpa/tasks/ad_spend`）：紫鸟浏览器自动抓取各站点日广告花费，输出汇总表
- **亚马逊评论抓取**（`rpa/tasks/amazon_review`）：读取 ASIN 列表抓取评论
- 统一契约：每个 flow 都是 `run(payload) -> {status, data, message}`，可供 Agent 直接调用；RPA 工具支持进程内调用或独立 MCP server（`MCP_RPA_URL`）

### RPA 任务队列（`src/agent/rpa_jobs.py`）

RPA 任务单次耗时 5~15 分钟，**绝不阻塞聊天回合**。采用 **DB 当队列 + 后台调度器** 架构：

- **提交即回执**：LLM 只见 3 个 `submit_rpa_*` 提交工具（`submit_rpa_query_campaign_spend` / `submit_rpa_collect_amazon_review` / `submit_rpa_update_track_table`），提交后**立即返回 job_id**，回合秒完；原始 `mcp_rpa_*` 工具经 `mcp_setup.setup_rpa_mcp(register=False)` **硬隐藏**，杜绝直接调用阻塞回合。
- **DB 当队列**：`rpa_jobs` 表（Postgres，`init_db()` 自动建表）持久化任务，状态 `queued → running → done/failed`，后端重启不丢。
- **后台调度器**：`get_rpa_dispatcher()` 单例随后端 lifespan 启动，每 2s `SELECT ... FOR UPDATE SKIP LOCKED` 认领任务、**一次一个串行执行**；启动时把残留 `running` 重置回 `queued`（at-least-once）。任务经 `MCPToolImporter.call_tool` 派发给独立 RPA MCP 执行器，结果回写 `result` / `error`。
- **状态 API**：`GET /api/rpa/jobs`（列表，`?limit=` / `?status=`）、`GET /api/rpa/jobs/{job_id}`（详情），均需登录。
- **前端面板**：`/rpa` 任务面板（`RPAJobsView.vue`）展示 job_id / 类型 / 状态 / 时间 / 结果 / 错误，5s 自动刷新。
- **冒烟约定**：`RPA_DRY_RUN=1` 时调度器跳过真实 MCP 调用、标记 done + 假结果（只读冒烟/演示用），见 `scripts/test_rpa_queue_smoke.py`。

**执行器**：RPA 与后端同一仓库，但运行在**独立 stdio 子进程**（`python -m skills.rpa.mcp_server`），按 RPA 意图惰性拉起、跨任务复用，与聊天回合完全隔离；未来多机部署可用 `MCP_RPA_URL` + `RPA_MCP_TOKEN` 走 HTTP 模式。

#### 可靠性加固
- **调用/连接超时**：调度器对「连接 + 单次 `call_tool`」分别用 `asyncio.wait_for` 掐超时，防 MCP 挂起占死单队列；超时后任务标 `failed`（error 提示到面板确认实际结果，避免副作用已完成被误当成功）。阈值 `RPA_EXEC_TIMEOUT_SECONDS`（默认 `1200`）。
- **僵死守护**：调度器每轮认领前执行 `_sweep_stale_running()`，把 `running` 超过 `RPA_JOB_MAX_RUNTIME_SECONDS`（默认 `1800`）且 `started_at` 非空的任务自动标 `failed`，不再只能靠重启恢复。
- **executor 退避重连**：RPA MCP 连接失败后按 `RPA_MCP_RECONNECT_BACKOFF_SECONDS`（默认 `60`）限频重试——不再"崩一次停摆到重启"。调度器 `call_tool` 遇连接级故障调用 `mcp_setup.evict_rpa()` 驱逐连接进入退避，下个任务自动重建；仅驱逐 RPA（`register=False`，无注册副作用），不影响已注册工具的其它 MCP 服务。
- **结果回流对话**：提交那一轮自动把 LangGraph 对话线程 ID 写入任务 `main_thread_id`；`get_rpa_job_status` 只读工具（挂在 core tools、viewer+ 可用、无需审批）供用户追问"任务完成了吗/结果如何"时查询——传 `job_id` 精确查，或**无参**自动解析「当前对话线程最近提交的任务」。前端无需为回流做改动。
- **新增环境变量**：`RPA_EXEC_TIMEOUT_SECONDS`(1200) / `RPA_JOB_MAX_RUNTIME_SECONDS`(1800) / `RPA_MCP_RECONNECT_BACKOFF_SECONDS`(60)。注意：`rpa_jobs` 新增列 `main_thread_id` 走 `Base.metadata.create_all`，**已有开发库需手动 `ALTER TABLE rpa_jobs ADD COLUMN main_thread_id VARCHAR(64)`**（或重建该表）后生效。

### 安全防护
- **InputGuard**：Regex + 可选 LLM 二分类检测 Prompt 注入和越狱尝试
- **OutputGuard**：PII 自动检测与脱敏（手机号/身份证/邮箱/银行卡）
- **RBAC**：admin / editor / viewer 三级权限，工具和知识库按角色隔离，/api/admin 角色管理端点
- **SSRF 防护**：URL 导入时校验协议/主机名/私有 IP
- **XSS 防护**：前端 dompurify 过滤 script/iframe/on* 属性/javascript: URL
- **路径遍历防护**：文件操作限制在 `UPLOAD_DIR` 白名单内

### 生产就绪
- **JWT 认证** + bcrypt 密码哈希
- **速率限制**：内存滑动窗口（可选 Redis 持久化）
- **成本追踪**：每步 LLM 调用的 token 消耗和延迟可观测（`observability/cost.py`），LangFuse 可接入
- **Docker 安全**：移除 docker.sock 挂载，禁用宿主机 PowerShell 执行

## 快速开始

### 环境要求

- Python ≥ 3.11（已适配 Python 3.14）
- Node.js ≥ 18（前端开发）
- Docker & Docker Compose（推荐）

### 1. 克隆并配置

```bash
git clone <repo-url>
cd langgraph-agent

# 复制环境变量模板
cp .env.example .env

# 编辑 .env，至少填入以下必需配置：
#   LLM_API_KEY      — LLM API 密钥
#   EMBEDDING_API_KEY — Embedding 服务 API 密钥
#   JWT_SECRET_KEY    — 生成: python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. 启动基础设施

```bash
# 启动 Qdrant + Neo4j + PostgreSQL + Redis
docker compose up -d qdrant neo4j postgres redis
```

### 3. 安装依赖

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装后端依赖（以 pyproject.toml 为准）
pip install -e .

# 安装前端依赖
cd frontend && npm install && cd ..
```

### 4. 启动服务

```bash
# 方式 A: 命令行模式（本地交互）
python src/main.py

# 方式 B: Web 服务模式
python run.py
# 访问 http://localhost:8080
# API 文档: http://localhost:8080/docs

# 方式 C: Docker 全栈部署
docker compose --profile full up -d
```

### 5. 构建前端（可选）

```bash
cd frontend && npm run build && cd ..
# 构建产物在 frontend/dist/，FastAPI 自动托管
```

## 配置参考

完整环境变量列表见 [`.env.example`](./.env.example)。

| 类别 | 变量 | 必需 | 说明 |
|------|------|------|------|
| LLM | `LLM_API_KEY` | ✓ | 主模型 API Key |
| LLM | `LLM_BASE_URL` | ✓ | API 端点（OpenAI 兼容） |
| LLM | `LLM_MODEL` | ✓ | 主模型名称（Pro） |
| LLM | `LLM_FLASH_MODEL` | ✓ | Flash 模型名称 |
| LLM | `LLM_FALLBACK_MODEL` | | 备用模型 1 / 2 |
| 视觉 | `VISION_ENABLED` / `VISION_MODEL` | | 图片 OCR 与理解（默认复用主 LLM） |
| Embedding | `EMBEDDING_API_KEY` | ✓ | Embedding 服务 API Key |
| Embedding | `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | ✓ | Embedding 端点与维度 |
| Rerank | `RERANK_API_KEY` / `RERANK_MODEL` | | RAG 精排（默认复用 Embedding Key） |
| 向量库 | `QDRANT_URL` | ✓ | Qdrant 服务地址 |
| 图库 | `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` | ✓ | Neo4j 连接地址 |
| 数据库 | `POSTGRES_URL` | | PostgreSQL（线程/记忆存储，可缺省） |
| 认证 | `JWT_SECRET_KEY` | ✓ | JWT 签名密钥 |
| 搜索 | `TAVILY_API_KEY` | | 网页搜索 |
| 限流/缓存 | `REDIS_URL` | | Redis（跨进程限流 + 语义缓存） |
| 审批 | `APPROVAL_MODE` / `APPROVAL_TTL_SECONDS` | | 审批模式与超时 |
| 可观测 | `LANGFUSE_*` | | LangFuse 追踪 |
| MCP | `MCP_*_URL` | | Playwright/RPA/Filesystem/Time/Docker/SearXNG 远程端点 |
| RPA | `ZINIAO_*` / `SHIMAOTONG_*` | | 紫鸟浏览器与世贸通凭证 |
| RPA | `AD_SPEND_*` / `AMAZON_REVIEW_*` | | 广告花费/评论任务配置 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/auth/register` | 用户注册 |
| `POST` | `/api/auth/login` | 用户登录 |
| `GET` | `/api/auth/me` | 当前用户信息 |
| `WS` | `/ws/chat` | WebSocket 流式对话（首条消息携带 token 认证） |
| `GET` | `/api/tools` | 工具列表（45+） |
| `GET` | `/api/skills` | 技能列表（18） |
| `GET` | `/api/kb/stats` | 知识库统计 |
| `POST` | `/api/kb/search` | 知识库检索（dense / hybrid） |
| `POST` | `/api/kb/upload` | 上传文档 |
| `GET` | `/api/kb/upload-progress/{task_id}` | 上传索引进度 |
| `POST` | `/api/kb/import-url` | URL 导入（SSRF 校验） |
| `GET` | `/api/kb/sources/{id}/chunks` | 来源分块 |
| `DELETE` | `/api/kb/sources/{id}` | 删除来源 |
| `POST` | `/api/kb/reindex` | 重建索引 |
| `GET` | `/api/memories` | 记忆列表 |
| `POST` | `/api/memories` | 添加记忆 |
| `DELETE` | `/api/memories` | 删除记忆 |
| `GET` | `/api/threads` | 对话线程列表 |
| `POST` | `/api/threads` | 创建对话 |
| `GET` | `/api/threads/{id}` | 对话历史 |
| `PATCH` | `/api/threads/{id}` | 修改线程（标题等） |
| `DELETE` | `/api/threads/{id}` | 删除对话 |
| `GET` / `POST` | `/api/approvals/{thread_id}` | 审批状态查询 / 审批操作 |
| `GET` | `/api/rpa/jobs` | RPA 任务列表（`?limit=` / `?status=`） |
| `GET` | `/api/rpa/jobs/{job_id}` | RPA 任务详情/结果 |
| `GET` | `/api/admin/users` | 用户列表（admin） |
| `PUT` | `/api/admin/users/{id}/role` | 修改角色（admin） |
| `GET` | `/api/health` | 健康检查 |

## 项目结构

```
langgraph-agent/
├── src/
│   ├── agent/            # LangGraph 图、工具、MCP、审批、摘要
│   │   ├── graph.py      # 核心 StateGraph（14 节点 + specialist 子图）
│   │   ├── state.py      # AgentState / SpecialistState 状态定义
│   │   ├── core.py       # 系统提示词 + 核心工具
│   │   ├── specialists.py # 多 Agent（研究员/代码/数据分析/通用）
│   │   ├── summarizer.py # 对话摘要器 + 上下文压缩
│   │   ├── context_budget.py # token 预算（借鉴 Codex harness）
│   │   ├── exec_policy.py    # 命令级执行策略（allow/prompt/forbidden）
│   │   ├── approval.py   # Human-in-the-Loop 审批
│   │   ├── vision.py     # 视觉模型（图片 OCR / 描述）
│   │   ├── rpa_jobs.py   # RPA 任务队列（submit_rpa_* + DB 当队列 + 后台调度器）
│   │   └── mcp_setup.py  # MCP 客户端管理（懒加载）
│   ├── api/              # FastAPI 服务
│   │   ├── server.py     # 应用入口 + 中间件
│   │   ├── routers/      # auth/chat/knowledge/threads/memories/approval/admin/rpa/tools
│   │   ├── database.py   # SQLAlchemy 异步引擎
│   │   └── rate_limit.py # 速率限制
│   ├── rag/              # 知识库
│   │   ├── indexer.py    # LightRAG 索引 + BM25 重建
│   │   ├── retrieval.py  # 混合检索（BM25 + dense + graph）+ RRF
│   │   ├── parsers.py    # 多格式文档解析器
│   │   └── embedding.py  # Embedding 工厂
│   ├── skills/           # 技能工具
│   │   ├── code_executor.py    # 沙箱代码执行（Docker）
│   │   ├── rpa/                # RPA 框架（紫鸟浏览器会话、Excel、检查点）
│   │   │   └── tasks/          # ad_spend / amazon_review 任务流
│   │   └── shimaotong/         # 世贸通抬头报关（验证码 + 订单构建）
│   │   └── rpa/mcp_server.py   # 独立 RPA MCP 执行器（stdio 子进程）
│   ├── context/          # 记忆系统（Mem0 + 情景记忆）
│   ├── security/         # 安全护栏（InputGuard/OutputGuard）
│   ├── auth/             # RBAC 权限
│   ├── observability/    # LangFuse + 成本追踪
│   ├── cache/            # 语义缓存
│   └── prompts/          # 提示词模板管理
├── frontend/             # Vue 3 + Vite + Tailwind CSS
│   └── src/views/        # ChatView / KBManagement / KnowledgeView / MemoriesView / RPAJobsView / Login / Register
├── tests/                # pytest 测试套件（25 文件 / 460+ 用例）
├── scripts/              # 评估、基准测试、冒烟验证脚本
├── docker-compose.yml    # 多服务编排
├── Dockerfile            # 生产镜像
├── pyproject.toml        # Python 项目配置（依赖唯一来源）
└── .env.example          # 环境变量模板
```

## 开发

### 运行测试

```bash
# 全部测试
pytest tests/ -v

# 跳过集成测试
pytest tests/ -v --ignore=tests/test_integration.py

# 真 Sub-Agent 子图专项（成本合并/报告提取/路由/审批门控/子图行为）
pytest tests/test_subagent.py -v
```

### 验证脚本

```bash
# 全功能冒烟（健康/RBAC/RAG/对话/线程/记忆/工具/审批，51 项断言）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/full_feature_test.py

# 模型路由/回退验证
.venv/Scripts/python.exe scripts/verify_model_routing.py

# KB 更新链路验证（upload→Neo4j 查边→reindex→删除）
.venv/Scripts/python.exe scripts/verify_kb_update.py

# RPA 任务队列全链路（DRY_RUN，自起后端 :8091，只读不触真实业务）
.venv/Scripts/python.exe scripts/test_rpa_queue_smoke.py

# RPA 懒连接 + mcp_rpa_* 硬隐藏契约（打 :8080，可用 RPA_SMOKE_BASE/WS 指向别端口）
.venv/Scripts/python.exe scripts/test_rpa_lazy_connect_api.py

# RPA MCP 连接层验证（importer 直连 + 工具名契约）
.venv/Scripts/python.exe scripts/test_rpa_mcp_connect.py
```

冒烟账号：`claude_verify` / `ClaudeVerify123!`。登录限流 10 次/分钟/IP，多次运行可携带 `X-Real-IP` 头绕过。

### 代码风格

项目遵循 PEP 8，使用中文注释和文档字符串。关键约定：
- 环境变量使用 `os.getenv("KEY")` 模式，无硬编码默认值（安全敏感项）
- AsyncOpenAI 客户端通过 `get_async_openai_client()` 工厂获取（避免重复创建）
- 同步 I/O（Mem0、文件解析）使用 `asyncio.to_thread()` 包装

## 部署

### Docker 部署

```bash
# 构建镜像
docker build -t langgraph-agent:latest .

# 全栈启动
docker compose --profile full up -d

# 仅 Agent + 核心基础设施
docker compose up -d qdrant neo4j postgres agent
```

### 安全注意事项

- `.env` 已在 `.gitignore` 中，不会被提交到 Git
- 部署后请立即更换 `JWT_SECRET_KEY`
- 在 LLM API 服务商后台配置 IP 白名单
- 生产环境建议启用 Redis（跨进程限流 + 语义缓存）
- Docker 部署时不挂载 `docker.sock`

## 技术栈

| 层 | 技术 |
|---|---|
| Agent 框架 | LangGraph (StateGraph) + langgraph-checkpoint (SQLite) |
| LLM | OpenAI 兼容 API（DeepSeek / DashScope，双模型 + 回退） |
| 后端 | FastAPI + Uvicorn + WebSocket |
| 前端 | Vue 3 + TypeScript + Vite + Tailwind CSS + Pinia |
| 向量库 | Qdrant |
| 图数据库 | Neo4j (LightRAG) |
| 关系数据库 | PostgreSQL（线程/记忆，SQLAlchemy async） |
| 检索 | BM25 (rank-bm25) + Dense + Graph，RRF + DashScope rerank |
| 记忆 | Mem0 |
| 缓存/限流 | Redis |
| 可观测 | LangFuse + 成本追踪 |
| MCP | Playwright / SearXNG / Filesystem / Sequential Thinking / Time / Docker / RPA |
| RPA | Playwright + playwright-stealth + ddddocr + OpenCV + Excel |
| 容器化 | Docker + Docker Compose |

## License

MIT
