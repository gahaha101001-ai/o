import time

from redis.asyncio import Redis


class OnlineUsersTracker:
    def __init__(self, redis_client: Redis, key: str, ttl_seconds: int) -> None:
        self.redis = redis_client
        self.key = key
        self.pages_key = f"{key}:pages"
        self.ttl_seconds = ttl_seconds

    async def _cleanup_stale_ids(self, min_alive: int) -> None:
        expired_visitor_ids = await self.redis.zrangebyscore(self.key, 0, min_alive)
        if expired_visitor_ids:
            await self.redis.hdel(self.pages_key, *[str(visitor_id) for visitor_id in expired_visitor_ids])
        await self.redis.zremrangebyscore(self.key, 0, min_alive)

    async def heartbeat(self, visitor_id: str, page_path: str) -> int:
        now = int(time.time())
        min_alive = now - self.ttl_seconds
        pipeline = self.redis.pipeline()
        pipeline.zadd(self.key, {visitor_id: now})
        pipeline.hset(self.pages_key, visitor_id, page_path)
        pipeline.zcard(self.key)
        _, _, count = await pipeline.execute()
        await self._cleanup_stale_ids(min_alive)
        return int(count)

    async def count(self) -> int:
        now = int(time.time())
        min_alive = now - self.ttl_seconds
        await self._cleanup_stale_ids(min_alive)
        count = await self.redis.zcard(self.key)
        return int(count)

    async def active_ids(self) -> list[str]:
        now = int(time.time())
        min_alive = now - self.ttl_seconds
        await self._cleanup_stale_ids(min_alive)
        visitor_ids = await self.redis.zrangebyscore(self.key, min_alive, "+inf")
        return [str(visitor_id) for visitor_id in visitor_ids]

    async def active_pages(self) -> dict[str, str]:
        visitor_ids = await self.active_ids()
        if not visitor_ids:
            return {}
        page_values = await self.redis.hmget(self.pages_key, visitor_ids)
        visitor_pages: dict[str, str] = {}
        for index, visitor_id in enumerate(visitor_ids):
            page_value = page_values[index] if index < len(page_values) else None
            normalized_path = str(page_value).strip() if page_value is not None else ""
            visitor_pages[str(visitor_id)] = normalized_path or "/"
        return visitor_pages
