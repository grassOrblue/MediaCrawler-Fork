"""Cooperative request pacing; never retries access restrictions."""
import asyncio
import time


class RequestPacer:
    def __init__(self, interval, rest_every=0, rest_seconds=0, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.interval = interval
        self.rest_every = rest_every
        self.rest_seconds = rest_seconds
        self.clock, self.sleep = clock, sleep
        self._lock = asyncio.Lock()
        self._next = 0.0
        self._count = 0
        self.blocked_reason = None

    async def wait(self):
        async with self._lock:
            if self.blocked_reason:
                return
            delay = max(0.0, self._next - self.clock())
            if delay:
                await self.sleep(delay)
            if self.blocked_reason:
                return
            self._count += 1
            rest = self.rest_seconds if self.rest_every and self._count % self.rest_every == 0 else 0
            self._next = self.clock() + max(self.interval, rest)

    def block(self, reason):
        self.blocked_reason = reason
