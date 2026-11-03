"""Відкладений запуск аналізу для кожного чату окремо (debounce з верхньою межею очікування)."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class _Pending:
    first: float
    urgent_deadline: float = math.inf
    timer: asyncio.Task | None = None


class Debouncer:
    """poke() відкладає запуск run(key), поки в чаті не затихне.

    Запуск відбудеться не пізніше max_wait від першого poke, а для термінових
    (до бота звернулися напряму) — за urgent_delay. Для одного ключа run
    ніколи не виконується паралельно.
    """

    def __init__(
        self,
        run: Callable[[int], Awaitable[None]],
        debounce: float,
        max_wait: float,
        urgent_delay: float = 2.0,
    ):
        self._run = run
        self.debounce = debounce
        self.max_wait = max_wait
        self.urgent_delay = urgent_delay
        self._pending: dict[int, _Pending] = {}
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task] = set()

    def poke(self, key: int, *, urgent: bool = False) -> None:
        now = time.monotonic()
        state = self._pending.setdefault(key, _Pending(first=now))
        if urgent:
            state.urgent_deadline = min(state.urgent_deadline, now + self.urgent_delay)
        deadline = min(now + self.debounce, state.first + self.max_wait, state.urgent_deadline)
        if state.timer is not None:
            state.timer.cancel()
        state.timer = asyncio.create_task(self._fire(key, max(0.0, deadline - now)))
        self._tasks.add(state.timer)
        state.timer.add_done_callback(self._tasks.discard)

    async def _fire(self, key: int, delay: float) -> None:
        await asyncio.sleep(delay)
        # Між пробудженням і pop немає await, тож новий poke уже створить новий таймер.
        self._pending.pop(key, None)
        async with self._locks[key]:
            try:
                await self._run(key)
            except Exception:
                log.exception("аналіз чату %s впав", key)
