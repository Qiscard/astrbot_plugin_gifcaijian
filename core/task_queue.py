"""Concurrency limiter + timeout for CPU/network heavy plugin tasks."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional, TypeVar

from astrbot.api import logger

T = TypeVar("T")


class TaskQueue:
    """
    - 限制同时执行的任务数（信号量）
    - 统一超时
    - 队列满时快速拒绝，避免堆积拖垮 bot
    """

    def __init__(
        self,
        max_concurrent: int = 2,
        default_timeout: float = 120.0,
        max_waiting: int = 8,
    ):
        self.max_concurrent = max(1, int(max_concurrent))
        self.default_timeout = float(default_timeout)
        self.max_waiting = max(0, int(max_waiting))
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._waiting = 0
        self._lock = asyncio.Lock()
        self._active = 0

    @property
    def status_text(self) -> str:
        return (
            f"并发:{self._active}/{self.max_concurrent} "
            f"排队:{self._waiting}/{self.max_waiting}"
        )

    async def run(
        self,
        coro_factory: Callable[[], Awaitable[T]],
        *,
        timeout: Optional[float] = None,
        name: str = "task",
    ) -> T:
        async with self._lock:
            if self._waiting + self._active >= self.max_concurrent + self.max_waiting:
                raise RuntimeError(
                    f"⏳ 任务队列已满({self.status_text})，请稍后再试"
                )
            self._waiting += 1

        try:
            await self._sem.acquire()
        finally:
            async with self._lock:
                self._waiting = max(0, self._waiting - 1)

        async with self._lock:
            self._active += 1

        timeout_s = self.default_timeout if timeout is None else float(timeout)
        try:
            logger.info(f"[gifcaijian] 开始任务 {name} ({self.status_text})")
            return await asyncio.wait_for(coro_factory(), timeout=timeout_s)
        except asyncio.TimeoutError as e:
            logger.warning(f"[gifcaijian] 任务超时 {name} > {timeout_s}s")
            raise TimeoutError(f"⏱ 任务超时（>{timeout_s:.0f}s）：{name}") from e
        finally:
            self._sem.release()
            async with self._lock:
                self._active = max(0, self._active - 1)
            logger.info(f"[gifcaijian] 结束任务 {name} ({self.status_text})")

    async def run_sync(
        self,
        func: Callable[..., T],
        *args: Any,
        timeout: Optional[float] = None,
        name: str = "cpu_task",
        **kwargs: Any,
    ) -> T:
        async def _factory() -> T:
            return await asyncio.to_thread(func, *args, **kwargs)

        return await self.run(_factory, timeout=timeout, name=name)
