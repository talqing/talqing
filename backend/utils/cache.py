"""Small process-local async cache helpers.

This is intentionally narrower than a general cache backend: values live only in
the current Python process, successful fetches are cached indefinitely, and
concurrent cold requests for the same key share one in-flight fetch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Generic, TypeVar, cast

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class AsyncMemoryCache(Generic[K]):
    """In-memory async get-or-set cache with per-key single-flight.

    Exceptions raised by `fetch` are not cached. This matches provider catalog
    use cases: a transient upstream failure should be retried by the next
    request, while a successful provider response can be reused for the process
    lifetime.
    """

    def __init__(self) -> None:
        self._values: dict[K, object] = {}
        self._locks: dict[K, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def get_or_set(self, key: K, fetch: Callable[[], Awaitable[V]]) -> V:
        if key in self._values:
            return cast(V, self._values[key])

        lock = await self._lock_for(key)
        async with lock:
            if key in self._values:
                return cast(V, self._values[key])
            value = await fetch()
            self._values[key] = value
            return value

    def get(self, key: K, expected_type: type[V]) -> V | None:
        value = self._values.get(key)
        return cast(V, value) if isinstance(value, expected_type) else None

    def set(self, key: K, value: object) -> None:
        self._values[key] = value

    def clear(self) -> None:
        self._values.clear()

    async def _lock_for(self, key: K) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock
