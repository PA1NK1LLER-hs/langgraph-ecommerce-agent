"""速率限制 — 滑动窗口 + 令牌桶混合，防止滥用。

WebSocket 和 REST 端点共用此模块。
支持两种后端：
- 内存（默认）：进程内滑动窗口，适合单进程部署
- Redis（可选）：分布式滑动窗口，跨进程/跨实例共享
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


class SlidingWindowLimiter:
    """固定窗口 + 滑动计数速率限制器（内存后端）。

    线程安全（依赖 GIL），适合单进程部署。
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._windows: dict[str, list[float]] = defaultdict(list)

    def _prune(self, key: str, now: float) -> None:
        """清理窗口之外的旧记录。"""
        cutoff = now - self.window_seconds
        records = self._windows[key]
        # 只在积累足够多时才清理，减少 GC 压力
        if len(records) > self.max_requests * 2:
            self._windows[key] = [t for t in records if t > cutoff]
        elif records and records[0] <= cutoff:
            # 找到第一个有效的位置
            idx = 0
            for i, t in enumerate(records):
                if t > cutoff:
                    idx = i
                    break
            self._windows[key] = records[idx:]

    def is_allowed(self, key: str) -> tuple[bool, float]:
        """检查 key 是否允许本次请求。

        Returns:
            (allowed, retry_after_seconds) — retry_after 为 0 时表示允许。
        """
        now = time.monotonic()
        self._prune(key, now)
        records = self._windows[key]

        if len(records) < self.max_requests:
            records.append(now)
            return True, 0.0

        # 计算最早请求过期的时间
        retry_after = self.window_seconds - (now - records[0])
        return False, max(retry_after, 0.5)

    def reset(self, key: str) -> None:
        self._windows.pop(key, None)


# ---------------------------------------------------------------------------
# Redis 后端
# ---------------------------------------------------------------------------


class RedisSlidingWindowLimiter:
    """基于 Redis Sorted Set 的分布式滑动窗口限流器。

    使用 Lua 脚本原子化执行 prune + check + add，保证跨进程一致性。
    当 Redis 不可用时自动降级到内存模式。
    """

    _LUA_SCRIPT = """
    local key = KEYS[1]
    local now = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local max_requests = tonumber(ARGV[3])
    local member = ARGV[4]

    -- 清理窗口外的旧记录
    local cutoff = now - window
    redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)

    -- 计数当前窗口内的请求
    local count = redis.call('ZCARD', key)
    if count < max_requests then
        redis.call('ZADD', key, now, member)
        redis.call('EXPIRE', key, math.ceil(window))
        return {1, 0}  -- allowed
    end

    -- 计算最早记录过期时间
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = window - (now - tonumber(oldest[2]))
    return {0, math.max(retry_after, 0.5)}
    """

    def __init__(self, max_requests: int, window_seconds: float, redis_url: str = ""):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._redis_url = redis_url
        self._redis = None
        self._lua_sha: str | None = None
        self._fallback = SlidingWindowLimiter(max_requests, window_seconds)

    async def _ensure_redis(self):
        """懒加载 Redis 连接。"""
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            # 预加载 Lua 脚本
            self._lua_sha = await self._redis.script_load(self._LUA_SCRIPT)
            logger.info("Redis 限流后端已连接: %s", self._redis_url)
        except ImportError:
            logger.warning("redis 包未安装，使用内存限流模式")
            self._redis = None
        except Exception as e:
            logger.warning("Redis 连接失败 (%s)，降级到内存限流模式", e)
            self._redis = None
        return self._redis

    async def is_allowed(self, key: str) -> tuple[bool, float]:
        """检查 key 是否允许本次请求（Redis sorted set 实现）。"""
        r = await self._ensure_redis()
        if r is None:
            # 降级到内存模式
            return self._fallback.is_allowed(key)

        import uuid
        now = time.time()
        member = f"{now}:{uuid.uuid4().hex[:8]}"

        try:
            result = await r.evalsha(
                self._lua_sha,
                1,
                f"rate_limit:{key}",
                str(now),
                str(self.window_seconds),
                str(self.max_requests),
                member,
            )
            allowed, retry_after = result[0], result[1]
            return bool(allowed), float(retry_after)
        except Exception as e:
            logger.warning("Redis 限流操作失败 (%s)，降级到内存模式", e)
            return self._fallback.is_allowed(key)

    def is_allowed_sync(self, key: str) -> tuple[bool, float]:
        """同步版（兼容非 async 调用）。"""
        return self._fallback.is_allowed(key)

    async def reset(self, key: str) -> None:
        r = await self._ensure_redis()
        if r:
            try:
                await r.delete(f"rate_limit:{key}")
            except Exception:
                pass
        self._fallback.reset(key)


# ---------------------------------------------------------------------------
# 限流器工厂
# ---------------------------------------------------------------------------


def create_redis_limiter(max_requests: int, window_seconds: float) -> RedisSlidingWindowLimiter | None:
    """创建 Redis 限流器（异步）。当 Redis 不可用时返回 None。"""
    from config import REDIS_URL
    if not REDIS_URL:
        return None
    return RedisSlidingWindowLimiter(max_requests, window_seconds, REDIS_URL)


# ── 预配置实例（始终使用内存后端，向后兼容）──

# WebSocket: 每连接每分钟最多 30 条消息
ws_limiter = SlidingWindowLimiter(max_requests=30, window_seconds=60.0)

# REST 认证端点: 每 IP 每分钟最多 10 次请求（防暴力破解）
auth_limiter = SlidingWindowLimiter(max_requests=10, window_seconds=60.0)

# 通用 API: 每 IP 每分钟最多 60 次请求
api_limiter = SlidingWindowLimiter(max_requests=60, window_seconds=60.0)


# ── 客户端 IP 提取（不盲信 X-Forwarded-For）──


def get_client_ip(request) -> str:
    """安全地从请求中提取客户端 IP。

    优先级：
    1. X-Real-IP（由可信反向代理 Nginx/Caddy 设置）
    2. request.client.host（直连 IP）
    3. X-Forwarded-For 仅取最左侧第一个（但标记为低信任度）

    注意：不信任客户端自行传入的 X-Forwarded-For。
    """
    # X-Real-IP 通常由可信反向代理设置，单个 IP
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # 直连 IP（无代理场景）
    if request.client and request.client.host:
        return request.client.host

    return "unknown"


# ── Redis 限流器（需配置 REDIS_URL，异步）──
_redis_ws_limiter: RedisSlidingWindowLimiter | None = None
_redis_auth_limiter: RedisSlidingWindowLimiter | None = None
_redis_api_limiter: RedisSlidingWindowLimiter | None = None


def get_redis_limiters() -> dict[str, RedisSlidingWindowLimiter | None]:
    """获取异步 Redis 限流器。首次调用时惰性初始化。"""
    global _redis_ws_limiter, _redis_auth_limiter, _redis_api_limiter
    if _redis_ws_limiter is None:
        _redis_ws_limiter = create_redis_limiter(30, 60.0)
        _redis_auth_limiter = create_redis_limiter(10, 60.0)
        _redis_api_limiter = create_redis_limiter(60, 60.0)
    return {
        "ws": _redis_ws_limiter,
        "auth": _redis_auth_limiter,
        "api": _redis_api_limiter,
    }


# ── 异步限流检查：Redis 优先，自动降级到内存 ──


async def _check_with_redis(
    redis_key: str,
    ip: str,
    fallback: SlidingWindowLimiter,
) -> tuple[bool, float]:
    """优先使用 Redis 限流，不可用时自动降级到内存模式。"""
    limiters = get_redis_limiters()
    redis_limiter = limiters.get(redis_key)
    if redis_limiter is not None:
        try:
            return await redis_limiter.is_allowed(ip)
        except Exception:
            pass  # 降级到内存
    return fallback.is_allowed(ip)


async def check_api_rate(ip: str) -> tuple[bool, float]:
    """检查通用 API 限流（Redis 优先 / 内存回退）。"""
    return await _check_with_redis("api", ip, api_limiter)


async def check_ws_rate(ip: str) -> tuple[bool, float]:
    """检查 WebSocket 限流（Redis 优先 / 内存回退）。"""
    return await _check_with_redis("ws", ip, ws_limiter)


async def check_auth_rate(ip: str) -> tuple[bool, float]:
    """检查认证端点限流（Redis 优先 / 内存回退）。"""
    return await _check_with_redis("auth", ip, auth_limiter)
