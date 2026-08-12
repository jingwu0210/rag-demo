"""Service Layer — RetrievalService（检索子链路编排）

编排 4 步检索流水线：Retriever(粗排) → Reranker(精排) → InjectionScanner(注入扫描)
- 阶段超时由 ResilienceGuard 管理（阶段键名 retrieval / rerank）
- rerank 超时或熔断 → degraded=True，粗排结果截断 top_n 兜底
- AdaptiveK 已在 Retriever 内部应用，Service 层不重复
- 对上层（ChatService）暴露单一 retrieve() 接口
"""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.config import ConfigRegistry
from core.guard import ResilienceGuard, StageTimeoutError
from core.logging_config import get_logger
from core.reranker import CircuitBreakerOpen, Reranker
from core.retriever import Retriever, ScoredDoc
from core.scanner import InjectionScanner

logger = get_logger(module="retrieval")

_guard_singleton: Optional[ResilienceGuard] = None


def get_guard_singleton() -> ResilienceGuard:
    """全局守卫单例：并发信号量在 Service 层共享（惰性构建，避免 import 期依赖 ConfigRegistry）"""
    global _guard_singleton
    if _guard_singleton is None:
        _guard_singleton = ResilienceGuard()
    return _guard_singleton


@dataclass
class RetrievalOutput:
    docs: List[ScoredDoc]
    mode: str                        # 实际执行的模式
    timing_ms: Dict[str, int] = field(default_factory=dict)   # {"retrieval","rerank","total"}
    degraded: bool = False           # 是否降级（rerank 超时/熔断）
    injection_blocked: int = 0


class RetrievalService:
    def __init__(self, retriever=None, reranker=None, scanner=None, guard=None):
        if retriever is None:
            from core.embedder import Embedder
            from storage.chroma_client import ChromaStore
            retriever = Retriever(ChromaStore(), Embedder())
        self.retriever = retriever
        self.reranker = reranker or Reranker()
        self.scanner = scanner or InjectionScanner()
        self.guard = guard or get_guard_singleton()
        # 专用线程池（设计文档 §3.3）：同步检索/精排移出事件循环线程，
        # 使阶段超时（asyncio.wait_for）在阻塞调用上可达（评审 I-1）
        self._retrieval_pool = ThreadPoolExecutor(
            max_workers=int(ConfigRegistry.get("retrieval.max_workers", 2)))
        self._rerank_pool = ThreadPoolExecutor(
            max_workers=int(ConfigRegistry.get("reranker.max_workers", 3)))

    async def _call(self, fn, pool, *args, **kwargs):
        """下层组件可能是异步（测试 mock）或同步（真实实现）— 统一适配。

        异步函数直接 await（AsyncMock 兼容）；同步函数提交到专用线程池执行，
        阻塞期间事件循环保持自由，阶段超时可达。
        注意：run_in_executor 仅支持位置参数，调用点须按函数签名传位置参数。
        """
        if asyncio.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(pool, fn, *args)

    async def retrieve(self, query: str, doc_type: str = None) -> RetrievalOutput:
        total_start = time.perf_counter()

        # Step 1: 粗排检索（阶段超时；超时 → 空结果继续，触发低置信拒答）
        t0 = time.perf_counter()
        result = None
        try:
            result = await self.guard.with_stage_timeout(
                "retrieval",
                self._call(self.retriever.retrieve, self._retrieval_pool,
                           query, 20, doc_type))
        except StageTimeoutError:
            logger.warning("retrieval_stage_timeout", query=query)
        retrieval_ms = int((time.perf_counter() - t0) * 1000)

        mode = result.mode if result else ConfigRegistry.get("retrieval.mode", "hybrid+rerank")
        candidates = result.docs if result else []
        degraded = False
        rerank_ms = 0

        # Step 2: 精排（reranker.enabled 且模式为 hybrid+rerank 时）
        if (result is not None and candidates
                and ConfigRegistry.get("reranker.enabled", True)
                and mode == "hybrid+rerank"):
            t1 = time.perf_counter()
            top_n = int(ConfigRegistry.get("reranker.top_n", 5))
            try:
                candidates = await self.guard.with_stage_timeout(
                    "rerank", self._call(self.reranker.rerank, self._rerank_pool,
                                         query, candidates))
            except (StageTimeoutError, CircuitBreakerOpen):
                # 降级：跳过 rerank，直接用粗排结果截断 top_n 兜底
                degraded = True
                logger.warning("rerank_degraded", query=query, top_n=top_n)
                candidates = candidates[:top_n]
            rerank_ms = int((time.perf_counter() - t1) * 1000)

        # Step 3: 注入扫描（AdaptiveK 已由 Retriever 应用，此处不重复）
        cleaned, blocked = self.scanner.scan(candidates)

        return RetrievalOutput(
            docs=cleaned,
            mode=mode,
            timing_ms={
                "retrieval": retrieval_ms,
                "rerank": rerank_ms,
                "total": int((time.perf_counter() - total_start) * 1000),
            },
            degraded=degraded,
            injection_blocked=blocked,
        )
