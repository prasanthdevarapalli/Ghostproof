"""Supabase + Redis singleton clients."""

from __future__ import annotations
import logging
from typing import Optional

from supabase import create_client, Client as SupabaseClient
import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

# ── Supabase ────────────────────────────────────────────────────────
_supabase_client: Optional[SupabaseClient] = None
_supabase_service: Optional[SupabaseClient] = None


def get_supabase() -> SupabaseClient:
    """Anon-key client (respects RLS)."""
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(settings.supabase_url, settings.supabase_key)
    return _supabase_client


def get_supabase_service() -> SupabaseClient:
    """Service-role client (bypasses RLS). Use for admin operations only."""
    global _supabase_service
    if _supabase_service is None:
        _supabase_service = create_client(
            settings.supabase_url, settings.supabase_service_key
        )
    return _supabase_service


# ── Redis ───────────────────────────────────────────────────────────
_redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> Optional[aioredis.Redis]:
    """Return async Redis client or None if unavailable."""
    global _redis_client
    if not settings.redis_url:
        return None
    if _redis_client is None:
        try:
            _redis_client = aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
            )
            await _redis_client.ping()
            logger.info("Redis connected")
        except Exception as e:
            logger.warning("Redis unavailable, caching disabled: %s", e)
            _redis_client = None
    return _redis_client


async def close_redis():
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None
