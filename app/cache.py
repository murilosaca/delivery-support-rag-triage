"""Redis cache with async and sync clients and a silent outage fallback.

The API never fails because Redis is down. A cache error becomes a miss on
read and a no-op on write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import redis

logger = logging.getLogger(__name__)

_TRUE_STRINGS = {"1", "true", "yes", "on"}
_OUTAGE_COOLDOWN_SECONDS = 30.0


def build_cache_key(user_message: str, order_value: float) -> str:
    """Build a SHA-256 cache key from the message and the order value.

    The digest covers only those two inputs. ``ticket_id`` is intentionally
    excluded so identical complaints share one decision.
    """

    normalized_message = " ".join(user_message.casefold().split())
    material = f"{normalized_message}|{order_value:.2f}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return f"triage:{digest}"


def _encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _decode(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Discarding a corrupt cache payload")
        return None
    if not isinstance(parsed, dict):
        logger.warning("Discarding a cache payload that is not a JSON object")
        return None
    return parsed


class TriageCache:
    """JSON cache over Redis, with both async and sync operations."""

    def __init__(self, redis_url: str, ttl_seconds: int) -> None:
        self._url = redis_url
        self._ttl = ttl_seconds
        self._async: redis.asyncio.Redis | None = None
        self._sync: redis.Redis | None = None
        self._logged_down = False
        self._down_until = 0.0

    def _client_kwargs(self) -> dict[str, Any]:
        return {
            "decode_responses": True,
            "socket_connect_timeout": 0.3,
            "socket_timeout": 0.3,
            "retry_on_timeout": False,
            "health_check_interval": 0,
        }

    def _circuit_open(self) -> bool:
        return time.monotonic() < self._down_until

    def _sync_client(self) -> redis.Redis:
        if self._sync is None:
            self._sync = redis.Redis.from_url(self._url, **self._client_kwargs())
        return self._sync

    def _async_client(self) -> redis.asyncio.Redis:
        if self._async is None:
            self._async = redis.asyncio.Redis.from_url(self._url, **self._client_kwargs())
        return self._async

    def _note_failure(self, operation: str, exc: Exception) -> None:
        self._down_until = time.monotonic() + _OUTAGE_COOLDOWN_SECONDS
        if not self._logged_down:
            logger.warning(
                "Redis %s failed (%s). Requests continue without cache.",
                operation,
                exc,
            )
            self._logged_down = True
            return
        logger.debug("Redis %s still failing: %s", operation, exc)

    def _note_success(self) -> None:
        self._down_until = 0.0
        if self._logged_down:
            logger.info("Redis connection recovered")
            self._logged_down = False

    def get_sync(self, key: str) -> dict[str, Any] | None:
        """Return a cached JSON object, or ``None`` on miss or outage."""

        if self._circuit_open():
            return None
        try:
            raw = self._sync_client().get(key)
        except Exception as exc:
            self._note_failure("get", exc)
            return None
        self._note_success()
        return _decode(raw if isinstance(raw, str) else None)

    def set_sync(self, key: str, payload: dict[str, Any]) -> None:
        """Store a JSON object. Failures are ignored."""

        if self._circuit_open():
            return
        try:
            self._sync_client().set(key, _encode(payload), ex=self._ttl)
        except Exception as exc:
            self._note_failure("set", exc)
            return
        self._note_success()

    def ping_sync(self) -> bool:
        """Return whether the sync client can reach Redis."""

        try:
            alive = bool(self._sync_client().ping())
        except Exception as exc:
            self._note_failure("ping", exc)
            return False
        self._note_success()
        return alive

    async def get(self, key: str) -> dict[str, Any] | None:
        """Async variant of :meth:`get_sync`."""

        if self._circuit_open():
            return None
        try:
            raw = await self._async_client().get(key)
        except Exception as exc:
            self._note_failure("get", exc)
            return None
        self._note_success()
        return _decode(raw if isinstance(raw, str) else None)

    async def set(self, key: str, payload: dict[str, Any]) -> None:
        """Async variant of :meth:`set_sync`."""

        if self._circuit_open():
            return
        try:
            await self._async_client().set(key, _encode(payload), ex=self._ttl)
        except Exception as exc:
            self._note_failure("set", exc)
            return
        self._note_success()

    async def ping(self) -> bool:
        """Return whether the async client can reach Redis."""

        try:
            alive = bool(await self._async_client().ping())
        except Exception as exc:
            self._note_failure("ping", exc)
            return False
        self._note_success()
        return alive

    async def close(self) -> None:
        """Close both clients. Close errors are swallowed."""

        if self._async is not None:
            try:
                closer = getattr(self._async, "aclose", None) or self._async.close
                result = closer()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.warning("Redis async client did not close cleanly: %s", exc)
            self._async = None
        if self._sync is not None:
            try:
                self._sync.close()
            except Exception as exc:
                logger.warning("Redis sync client did not close cleanly: %s", exc)
            self._sync = None


def cache_bypass_requested(header_value: str | None) -> bool:
    """Return whether the caller asked to skip the cache read."""

    if header_value is None:
        return False
    return header_value.strip().casefold() in _TRUE_STRINGS
