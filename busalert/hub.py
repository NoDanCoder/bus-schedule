from __future__ import annotations

import asyncio
from collections import defaultdict


class Hub:
    """Per-client fan-out for server-sent events. Slow consumers lose the oldest message."""

    def __init__(self):
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, client_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subs[client_id].add(q)
        return q

    def unsubscribe(self, client_id: str, q: asyncio.Queue) -> None:
        self._subs[client_id].discard(q)
        if not self._subs[client_id]:
            del self._subs[client_id]

    def has_subscribers(self, client_id: str) -> bool:
        return bool(self._subs.get(client_id))

    def publish(self, client_id: str, event: str, data: dict) -> None:
        for q in list(self._subs.get(client_id, ())):
            if q.full():
                q.get_nowait()
            q.put_nowait((event, data))
