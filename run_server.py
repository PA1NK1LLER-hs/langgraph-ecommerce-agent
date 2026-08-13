"""PyCharm 一键启动入口 — API Server + 前端 SPA。

在 PyCharm 中右键此文件 → Run / Debug 即可。
环境变量自动从项目根目录的 .env 文件加载。
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "src.api.server:app",
        host="0.0.0.0",
        port=8080,
        ws="wsproto",
        reload=True,
        log_level="info",
    )
