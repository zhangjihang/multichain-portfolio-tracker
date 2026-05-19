"""Rate limiting utilities."""

import asyncio
import time
from collections import deque


class RateLimiter:
    """Simple rate limiter using a sliding window."""

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request can be made within rate limits."""
        async with self._lock:
            now = time.monotonic()

            # Remove timestamps outside the window
            while self._timestamps and now - self._timestamps[0] > self.window_seconds:
                self._timestamps.popleft()

            if len(self._timestamps) >= self.max_requests:
                # Wait until the oldest request exits the window
                sleep_time = self.window_seconds - (now - self._timestamps[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                self._timestamps.popleft()

            self._timestamps.append(time.monotonic())

    async def __aenter__(self) -> "RateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *args) -> None:
        pass
