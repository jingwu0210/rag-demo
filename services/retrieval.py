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
from core.logging_config import get_logger, get_request_id
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
    # R3: 向量路 top1 余弦分数旁路透传（RefusalCheck 置信度判定用）
    vector_top1_sim: Optional[float] = None


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

    async def retrieve(self, query: str, doc_type: str = None,
                       mode: Optional[str] = None) -> RetrievalOutput:
        """检索编排。mode=None → config 全局值；否则请求级覆盖（/chat 的 mode 参数透传）。"""
        total_start = time.perf_counter()

        # 读取当前模式，决定粗排是否跳过 AdaptiveK（R7: rerank 候选池独立）
        mode_cfg = mode or ConfigRegistry.get("retrieval.mode", "hybrid+rerank")
        need_rerank = (mode_cfg == "hybrid+rerank"
                       and ConfigRegistry.get("reranker.enabled", True))
        if need_rerank:
            # rerank 候选池：粗排不截断，候选数 = top_k × candidates_multiplier（设计 §4.4）
            # fallback 默认值与 config.yaml 一致（1.5 → 30 候选）；config 为唯一真源
            coarse_top_k = int(ConfigRegistry.get("retrieval.vector.top_k", 20)
                               * ConfigRegistry.get("reranker.candidates_multiplier", 1.5))
        else:
            coarse_top_k = 20

        # Step 1: 粗排检索（阶段超时；超时 → 空结果继续，触发低置信拒答）
        t0 = time.perf_counter()
        result = None
        try:
            result = await self.guard.with_stage_timeout(
                "retrieval",
                self._call(self.retriever.retrieve, self._retrieval_pool,
                           query, coarse_top_k, doc_type, need_rerank, mode_cfg))
        except StageTimeoutError:
            logger.warning("retrieval_stage_timeout", query=query)
        retrieval_ms = int((time.perf_counter() - t0) * 1000)

        mode = result.mode if result else mode_cfg
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
                # 预热在阶段超时之外：首次加载模型耗时 >2s，会误触 stage_timeout
                # 导致永远冷启动降级。预热只在模型未加载时发生（幂等）。
                warmup = self._call(self.reranker.ensure_loaded, self._rerank_pool)
                await asyncio.wait_for(warmup, timeout=180)
                # P1h 双通道 RRF 二次融合（R4 排序失真修复：精排不独裁）：
                # 1) 记录粗排位次 = rerank 前候选列表 index + 1（rank 基准 1-based，
                #    与 core/fusion.py 的 1/(k+rank+1) 一致）
                coarse_rank = {d.chunk_id: i + 1 for i, d in enumerate(candidates)}
                # 2) rerank 全量排序（top_n=None 不截断），rerank 位次 = 全排序 index + 1
                ranked = await self.guard.with_stage_timeout(
                    "rerank", self._call(self.reranker.rerank, self._rerank_pool,
                                         query, candidates, None))
                rerank_rank = {d.chunk_id: i + 1 for i, d in enumerate(ranked)}
                # 3) 二次 RRF：final = 1/(rrf_k+粗排位次) + 1/(rrf_k+rerank位次)
                #    rrf_k 与粗排第一次融合共用 retrieval.fusion.rrf_k（config 单源，铁律 4）
                rrf_k = int(ConfigRegistry.get("retrieval.fusion.rrf_k", 60))
                for doc in candidates:
                    doc.score = (1.0 / (rrf_k + coarse_rank[doc.chunk_id])
                                 + 1.0 / (rrf_k + rerank_rank.get(
                                     doc.chunk_id, len(ranked) + 1)))
                # 4) 按 final 降序截取 top_n 作为最终结果
                candidates = sorted(candidates, key=lambda d: d.score,
                                    reverse=True)[:top_n]
            except (StageTimeoutError, CircuitBreakerOpen):
                # 降级：跳过 rerank，直接用粗排结果截断 top_n 兜底
                degraded = True
                logger.warning("rerank_degraded", query=query, top_n=top_n)
                candidates = candidates[:top_n]
            except Exception:
                # 预热失败（模型不可用等）：降级不重排，但记录错误
                degraded = True
                logger.warning("rerank_warmup_failed", query=query, exc_info=True)
                candidates = candidates[:top_n]
            rerank_ms = int((time.perf_counter() - t1) * 1000)

        # Step 3: 注入扫描（AdaptiveK 已由 Retriever 应用，此处不重复）
        cleaned, blocked = self.scanner.scan(candidates)

        # ── L2 埋点: retrieval_complete（粗排阶段，所有模式）──
        # request_id 来自 structlog contextvars（API 层 bind_contextvars 注入）；
        # 直接调用场景（eval/smoke）为 None，靠 run_id 追踪
        rid = get_request_id()
        logger.info("retrieval_complete",
                    request={"id": rid},
                    retrieval={
                        "mode": mode,
                        "coarse_candidates": len(result.docs) if result else 0,
                        "final_chunks": len(cleaned),
                        "top1_score": (round(float(cleaned[0].score), 4)
                                       if cleaned else None),
                        "vector_top1_sim": (round(float(result.vector_top1_sim), 4)
                                            if result and result.vector_top1_sim is not None
                                            else None),
                        "doc_type_filter": doc_type,
                        "injection_blocked": blocked,
                        "latency_ms": retrieval_ms,
                    },
                    chunks=[{
                        "heading_path": (d.metadata or {}).get("heading_path", ""),
                        "score": round(float(d.score), 4),
                        "source_file": (d.metadata or {}).get("source_file", ""),
                    } for d in cleaned[:5]])

        # ── L2 埋点: rerank_complete（精排阶段，仅执行了精排时）──
        if rerank_ms > 0 or degraded:
            logger.info("rerank_complete",
                        request={"id": rid},
                        rerank={
                            "candidates": len(result.docs) if result else 0,
                            "kept": len(cleaned),
                            "top1_score": (round(float(cleaned[0].score), 4)
                                           if cleaned else None),
                            "latency_ms": rerank_ms,
                        })

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
            vector_top1_sim=(result.vector_top1_sim if result else None),
        )
