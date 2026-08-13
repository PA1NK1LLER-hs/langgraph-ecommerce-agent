"""启动入口 — Python 3.14 兼容补丁 + 服务器启动。

用法: python run.py
"""

import asyncio
import asyncio.timeouts
import sys

# ═══════════════════════════════════════════════════
# Python 3.14 兼容补丁：替换整个 Timeout 类
# Python 3.14 的 Timeout.__aenter__ 要求必须在 task 内调用，
# 但 websockets/wsproto/httpx 在非 task 上下文中也会调用。
# 这里用旧版兼容 Timeout 类替换。
# ═══════════════════════════════════════════════════

_OldTimeout = asyncio.timeouts.Timeout


class Timeout(_OldTimeout):
    """Python 3.12 兼容的 Timeout — 不检查当前 task，安全处理超时回调。"""

    async def __aenter__(self):
        """绕过 Python 3.14 的 task 检查。"""
        if self._state is not getattr(asyncio.timeouts, '_State').CREATED:
            raise RuntimeError("Timeout has already been entered")
        self._state = getattr(asyncio.timeouts, '_State').ENTERED
        self._task = None  # 不绑定 task
        self._cancelling = None
        self.reschedule(self._when)
        return self

    def _on_timeout(self):
        """覆盖 3.14 的 _on_timeout，兼容 self._task 为 None 的情况。"""
        if self._task is None:
            return  # 没有 task 可取消，静默忽略
        try:
            self._task.cancel()
        except Exception:
            pass


# 替换 Timeout 类和 timeout 函数使用的类
asyncio.timeouts.Timeout = Timeout
# 确保 timeout 函数返回新 Timeout 实例（timeout 函数通过模块查找 Timeout）
# 同时替换 asyncio.timeout 便捷函数
asyncio.timeout = asyncio.timeouts.timeout

print("[run.py] Python 3.14 Timeout compatibility patch applied", flush=True)

# ═══════════════════════════════════════════════════
# 正常启动
# ═══════════════════════════════════════════════════
import uvicorn
from src.api.server import app

if __name__ == "__main__":
    print("=" * 50)
    print("  LangGraph Agent Server")
    print("  http://localhost:8080")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
