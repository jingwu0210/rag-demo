"""并发与超时守卫（ResilienceGuard）

- Semaphore 安全上限（concurrency.max_requests=10）：超限立返 429 语义（不排队），防雪崩 OOM
- 阶段级超时（retrieval/rerank/generation）：超时记 warning 并抛 StageTimeoutError
- 请求级硬超时（concurrency.request_timeout=9）：超时记 warning 并返回 {"partial": True, "timeout": True}
"""
from __future__ import annotations

import asyncio

from core.config import ConfigRegistry
from core.logging_config import get_logger

logger = get_logger(module=__name__)


class StageTimeoutError(Exception):
    """阶段级超时（retrieval / rerank / generation）"""

    def __init__(self, stage: str):
        self.stage = stage
        super().__init__(f"stage timeout: {stage}")


class ConcurrencyLimitExceeded(Exception):
    """并发槽位已满（超限立即拒绝，不排队）"""


class ResilienceGuard:
    def __init__(self):
        # 阶段超时：stage → config 路径（设计文档 §4.8）
        self.stage_timeouts = {
            "retrieval": ConfigRegistry.get("retrieval.timeout", 3),
            "rerank": ConfigRegistry.get("reranker.timeout", 2),
            "generation": ConfigRegistry.get("llm.timeout", 5),
        }
        self.request_timeout = ConfigRegistry.get("concurrency.request_timeout", 9)
        self._max_requests = int(ConfigRegistry.get("concurrency.max_requests", 10))
        self._semaphore = None

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """并发信号量（惰性创建：Python 3.9 的 Semaphore 构造必须绑定运行中的 event loop）"""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_requests)
        return self._semaphore

    async def acquire(self) -> asyncio.Semaphore:
        """获取并发槽位：立即返回不排队；锁已满时抛 ConcurrencyLimitExceeded"""
        sem = self.semaphore
        if sem.locked():
            raise ConcurrencyLimitExceeded("系统繁忙，请稍后重试")
        return sem

    async def with_stage_timeout(self, stage: str, coro):
        """阶段级超时保护：超时记 warning 日志 → 抛 StageTimeoutError(stage)"""
        timeout = self.stage_timeouts.get(stage, 10)
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("stage_timeout", stage=stage, timeout=timeout)
            raise StageTimeoutError(stage) from None

    async def with_request_timeout(self, coro):
        """请求级硬超时：超时记 warning 日志 → 返回 {"partial": True, "timeout": True}（dict 非异常）"""
        try:
            return await asyncio.wait_for(coro, timeout=self.request_timeout)
        except asyncio.TimeoutError:
            logger.warning("request_timeout", timeout=self.request_timeout)
            return {"partial": True, "timeout": True}
