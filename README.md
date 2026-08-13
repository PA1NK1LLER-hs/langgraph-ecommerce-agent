# LangGraph Agent

基于 [LangGraph](https://github.com/langchain-ai/langgraph) 构建的生产级 AI Agent，支持多工具调用、知识库检索、Human-in-the-Loop 审批、多 Agent 协作和 RPA 自动化。前端采用 Vue 3 + TypeScript，后端使用 FastAPI + WebSocket 流式对话。

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend (Vue 3)                         │
│                   WebSocket 流式对话 + KB 管理                     │
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
│  classify_intent → supervisor → planner → agent → tools          │
│       │                 │          │         │                   │
│       ▼                 ▼          ▼         ▼                   │
│  query_rewrite    specialists  replan  check_approval            │
│       │            (4 types)            (Human-in-Loop)          │
│       ▼                                                        │
│  search_rag ─────────────────────────────────────────────────   │
│                                                                  │
│  Flash LLM: 意图分类 / 查询改写 / 摘要                             │
│  Pro LLM:    推理 / 规划 / 工具调用 / 代码生成                      │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                      Infrastructure                              │
│  Qdrant (向量) · Neo4j (图) · PostgreSQL (checkpoint)           │
│  Redis (限流/缓存) · LightRAG (知识图谱) · Mem0 (记忆)             │
│  Playwright MCP · SearXNG · LangFuse (可观测)                    │
└─────────────────────────────────────────────────────────────────┘
```

## 核心特性

### Agent 能力
- **双模型架构**：Flash 模型处理意图分类、查询改写、对话摘要；Pro 模型处理复杂推理和工具调用
- **意图路由**：自动识别 8 种意图（闲聊/知识检索/联网搜索/文件操作/代码执行/RPA/时间/复杂任务），跳过不必要的处理节点
- **Plan-Execute-Replan**：复杂任务自动生成结构化执行计划，失败后自动重新规划
- **Supervisor 模式**：跨领域任务自动委派给专业子 Agent（研究员/代码专家/数据分析师/通用助手）
- **Human-in-the-Loop**：高风险操作（代码执行、文件修改）自动触发审批中断，支持 WebSocket 实时审批
- **对话摘要**：长对话自动压缩早期消息为结构化摘要，防止上下文溢出

### 知识库
- **混合检索**：Dense Vector + Sparse Vector (SPLADE) + Graph 三路并行检索，RRF 融合排序
- **多格式解析**：PDF / DOCX / XLSX / PPTX / HTML / Markdown / 图片（Vision API）自动解析入库
- **来源引用**：RAG 回答自动附带 `[来源: 文件名, 相关度: 0.XX]` 格式的精确引用
- **语义缓存**：相似问题复用缓存结果，降低 30-50% API 成本

### 安全防护
- **InputGuard**：Regex + LLM 二分类检测 Prompt 注入和越狱尝试
- **OutputGuard**：PII 自动检测与脱敏（手机号/身份证/邮箱/银行卡）
- **RBAC**：admin / editor / viewer 三级权限，工具和知识库按角色隔离
- **SSRF 防护**：URL 导入时校验协议/主机名/私有 IP
- **XSS 防护**：前端 sanitizeHtml 过滤 script/iframe/on* 属性/javascript: URL
- **路径遍历防护**：文件操作限制在 `UPLOAD_DIR` 白名单内

### 生产就绪
- **JWT 认证** + bcrypt 密码哈希
- **速率限制**：内存滑动窗口（可选 Redis 持久化）
- **模型回退**：主模型故障时自动切换备用模型
- **成本追踪**：每步 LLM 调用的 token 消耗和延迟可观测
- **Docker 安全**：移除 docker.sock 挂载，禁用宿主机 PowerShell 执行

## 快速开始

### 环境要求

- Python ≥ 3.11
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
# 启动 Qdrant + Neo4j + PostgreSQL
docker compose up -d qdrant neo4j postgres
```

### 3. 安装依赖

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装后端依赖
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
| LLM | `LLM_MODEL` | ✓ | 主模型名称 |
| LLM | `LLM_FLASH_MODEL` | ✓ | Flash 模型名称 |
| Embedding | `EMBEDDING_API_KEY` | ✓ | Embedding 服务 API Key |
| Embedding | `EMBEDDING_BASE_URL` | ✓ | Embedding 端点 |
| 向量库 | `QDRANT_URL` | ✓ | Qdrant 服务地址 |
| 图库 | `NEO4J_URI` | ✓ | Neo4j 连接地址 |
| 数据库 | `POSTGRES_URL` | ✓ | PostgreSQL 连接串 |
| 认证 | `JWT_SECRET_KEY` | ✓ | JWT 签名密钥 |
| 搜索 | `TAVILY_API_KEY` | | 网页搜索 |
| 限流 | `REDIS_URL` | | Redis（跨进程限流） |
| 可观测 | `LANGFUSE_PUBLIC_KEY` | | LangFuse 追踪 |
| RPA | `ZINIAO_COMPANY` | | 紫鸟浏览器公司名 |
| RPA | `ZINIAO_USERNAME` | | 紫鸟浏览器用户名 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/auth/register` | 用户注册 |
| `POST` | `/api/auth/login` | 用户登录 |
| `GET` | `/api/auth/me` | 当前用户信息 |
| `WS` | `/ws/chat` | WebSocket 流式对话 |
| `GET` | `/api/tools` | 工具列表 |
| `GET` | `/api/skills` | 技能列表 |
| `GET` | `/api/kb/stats` | 知识库统计 |
| `POST` | `/api/kb/search` | 知识库检索 |
| `POST` | `/api/kb/upload` | 上传文档 |
| `POST` | `/api/kb/import-url` | URL 导入 |
| `GET` | `/api/memories` | 记忆列表 |
| `POST` | `/api/memories` | 添加记忆 |
| `DELETE` | `/api/memories` | 删除记忆 |
| `GET` | `/api/threads` | 对话线程列表 |
| `POST` | `/api/threads` | 创建对话 |
| `GET` | `/api/threads/{id}` | 对话历史 |
| `DELETE` | `/api/threads/{id}` | 删除对话 |
| `POST` | `/api/approvals/{id}` | 审批操作 |
| `GET` | `/api/health` | 健康检查 |

## 项目结构

```
langgraph-agent/
├── src/
│   ├── agent/            # LangGraph 图、工具、MCP、审批、摘要
│   │   ├── graph.py      # 核心 StateGraph（14 节点）
│   │   ├── core.py       # 系统提示词 + 20+ 核心工具
│   │   ├── summarizer.py # 对话摘要器
│   │   ├── approval.py   # Human-in-the-Loop 审批
│   │   └── mcp_setup.py  # MCP 客户端管理
│   ├── api/              # FastAPI 服务
│   │   ├── server.py     # 应用入口 + 中间件
│   │   ├── models/       # SQLAlchemy 数据模型
│   │   ├── routers/      # 路由（auth/chat/knowledge/memories/threads/approval）
│   │   ├── deps.py       # JWT 依赖注入
│   │   └── rate_limit.py # 速率限制
│   ├── rag/              # 知识库
│   │   ├── indexer.py    # LightRAG 索引管理
│   │   ├── retrieval.py  # 混合检索 + RRF 融合
│   │   ├── parsers.py    # 多格式文档解析器
│   │   └── embedding.py  # Embedding 工厂
│   ├── skills/           # 技能工具
│   │   ├── code_executor.py    # 沙箱代码执行
│   │   └── rpa_ziniao.py       # 紫鸟浏览器 RPA
│   ├── context/          # 记忆系统（Mem0）
│   ├── security/         # 安全护栏（InputGuard/OutputGuard）
│   ├── auth/             # RBAC 权限
│   ├── observability/    # LangFuse + 成本追踪
│   ├── cache/            # 语义缓存
│   └── prompts/          # 提示词模板管理
├── frontend/             # Vue 3 + Vite + Tailwind CSS
│   └── src/views/        # ChatView / KBManagement / Login / Register
├── tests/                # pytest 测试套件（350+ tests）
├── scripts/              # 评估、基准测试脚本
├── docker-compose.yml    # 多服务编排
├── Dockerfile            # 生产镜像
├── pyproject.toml        # Python 项目配置
└── .env.example          # 环境变量模板
```

## 开发

### 运行测试

```bash
# 全部测试
pytest tests/ -v

# 跳过集成测试
pytest tests/ -v --ignore=tests/test_integration.py

# 单个测试文件
pytest tests/test_agent.py -v
```

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
| Agent 框架 | LangGraph (StateGraph) |
| LLM | OpenAI 兼容 API（DeepSeek / DashScope / MiMo / TokenPlan） |
| 后端 | FastAPI + Uvicorn + WebSocket |
| 前端 | Vue 3 + TypeScript + Vite + Tailwind CSS |
| 向量库 | Qdrant |
| 图数据库 | Neo4j (LightRAG) |
| 关系数据库 | PostgreSQL (SQLAlchemy async) |
| 记忆 | Mem0 |
| 缓存 | Redis |
| 可观测 | LangFuse |
| MCP | Playwright / SearXNG / Filesystem / Sequential Thinking |
| 容器化 | Docker + Docker Compose |

## License

MIT
