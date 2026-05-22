"""
Redis storage — multi-channel model.

Key schema
──────────
tw:channels                        SET   — all known channel IDs (strings)
tw:accounts:<channel_id>           SET   — usernames tracked in that channel
tw:seen:<channel_id>:<username>    SET   — tweet IDs already posted to that channel
"""

import logging
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

CHANNELS_KEY   = "tw:channels"
ACCOUNTS_PFX   = "tw:accounts:"
SEEN_PFX       = "tw:seen:"
MAX_SEEN       = 500   # max tweet IDs stored per (channel, account) pair


class RedisStorage:
    def __init__(self, redis_url: str):
        self._url   = redis_url
        self._redis: aioredis.Redis | None = None

    async def _r(self) -> aioredis.Redis:
        if self._redis is None:
            tls_kwargs = {"ssl_cert_reqs": None} if self._url.startswith("rediss://") else {}
            self._redis = await aioredis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=10,
                **tls_kwargs,
            )
            logger.info("Connected to Redis at %s", self._url.split("@")[-1])
        return self._redis

    # ── channels ─────────────────────────────────────────────────────────────

    async def get_channels(self) -> list[str]:
        r = await self._r()
        return list(await r.smembers(CHANNELS_KEY))

    async def register_channel(self, channel_id: str) -> bool:
        """Add channel to the known set. Returns True if newly added."""
        r = await self._r()
        added = await r.sadd(CHANNELS_KEY, str(channel_id))
        return bool(added)

    async def unregister_channel(self, channel_id: str) -> bool:
        """Remove channel and all its data. Returns True if it existed."""
        r       = await self._r()
        cid     = str(channel_id)
        existed = await r.srem(CHANNELS_KEY, cid)
        if existed:
            accounts = await self.get_accounts(cid)
            pipe = r.pipeline()
            pipe.delete(ACCOUNTS_PFX + cid)
            for username in accounts:
                pipe.delete(f"{SEEN_PFX}{cid}:{username}")
            await pipe.execute()
            logger.info("Unregistered channel %s", cid)
        return bool(existed)

    # ── accounts (per channel) ────────────────────────────────────────────────

    async def get_accounts(self, channel_id: str) -> list[str]:
        r = await self._r()
        return sorted(await r.smembers(ACCOUNTS_PFX + str(channel_id)))

    async def add_account(self, channel_id: str, username: str) -> bool:
        username = username.lower().lstrip("@")
        r        = await self._r()
        cid      = str(channel_id)
        await r.sadd(CHANNELS_KEY, cid)          # ensure channel is registered
        added = await r.sadd(ACCOUNTS_PFX + cid, username)
        if added:
            logger.info("Channel %s: added @%s", cid, username)
        return bool(added)

    async def remove_account(self, channel_id: str, username: str) -> bool:
        username = username.lower().lstrip("@")
        r        = await self._r()
        cid      = str(channel_id)
        removed  = await r.srem(ACCOUNTS_PFX + cid, username)
        if removed:
            await r.delete(f"{SEEN_PFX}{cid}:{username}")
            logger.info("Channel %s: removed @%s", cid, username)
        return bool(removed)

    # ── seen tweets (per channel + account) ──────────────────────────────────

    async def is_seen(self, channel_id: str, username: str, tweet_id: str) -> bool:
        r   = await self._r()
        key = f"{SEEN_PFX}{channel_id}:{username.lower()}"
        return bool(await r.sismember(key, tweet_id))

    async def mark_seen(self, channel_id: str, username: str, tweet_id: str):
        r    = await self._r()
        key  = f"{SEEN_PFX}{channel_id}:{username.lower()}"
        await r.sadd(key, tweet_id)
        size = await r.scard(key)
        if size > MAX_SEEN:
            for _ in range(size - MAX_SEEN):
                await r.spop(key)

    # ── stats ─────────────────────────────────────────────────────────────────

    async def channel_stats(self, channel_id: str) -> dict:
        cid      = str(channel_id)
        accounts = await self.get_accounts(cid)
        r        = await self._r()
        seen     = 0
        for username in accounts:
            seen += await r.scard(f"{SEEN_PFX}{cid}:{username}")
        return {"accounts": len(accounts), "seen": seen}

    async def global_stats(self) -> dict:
        channels = await self.get_channels()
        total_accounts, total_seen = 0, 0
        for cid in channels:
            s = await self.channel_stats(cid)
            total_accounts += s["accounts"]
            total_seen     += s["seen"]
        return {
            "channels": len(channels),
            "accounts": total_accounts,
            "seen":     total_seen,
        }
