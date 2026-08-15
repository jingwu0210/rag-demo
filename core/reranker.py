"""精排模块：Reranker（Cross-Encoder 重排）+ RerankerCircuitBreaker（三态熔断降级）"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, List, Optional

from core.config import ConfigRegistry
from core.retriever import ScoredDoc


class CircuitBreakerOpen(Exception):
    """熔断打开异常：调用方捕获后跳过 rerank，直接截断粗排结果降级"""

    pass


# 哨兵值：区分 rerank 的"未传 top_n"（用 config reranker.top_n 截断，默认行为）
# 与"显式传 top_n=None"（全量排序不截断 — P1h 二次融合需要完整 rerank 位次）
_RANK_DEFAULT = object()


class RerankerCircuitBreaker:
    """三态熔断器：CLOSED → OPEN → HALF_OPEN → CLOSED

    - CLOSED:  执行 fn；异常时 failure_count += 1，达到 failure_threshold → OPEN
    - OPEN:    抛 CircuitBreakerOpen；超过 recovery_timeout → HALF_OPEN
    - HALF_OPEN: 放行一次试探，成功 → CLOSED 并重置计数；失败 → 回 OPEN
    """

    def __init__(self, failure_threshold: Optional[int] = None,
                 recovery_timeout: Optional[float] = None):
        # 未显式传入时默认从 config reranker.circuit_breaker.* 读取（3 / 60）
        self.failure_threshold = (ConfigRegistry.get(
            "reranker.circuit_breaker.failure_threshold", 3)
            if failure_threshold is None else failure_threshold)
        self.recovery_timeout = (ConfigRegistry.get(
            "reranker.circuit_breaker.recovery_timeout", 60)
            if recovery_timeout is None else recovery_timeout)
        self.failure_count = 0
        self._state = "CLOSED"
        self._opened_at: Optional[float] = None  # 打开时刻，用于 recovery 判断

    @property
    def state(self) -> str:
        return self._state

    def call(self, fn: Callable[[], Any]) -> Any:
        if self._state == "OPEN":
            if self._recovery_elapsed():
                self._state = "HALF_OPEN"
            else:
                raise CircuitBreakerOpen("Reranker 熔断中, 使用 hybrid 降级")
        try:
            result = fn()
        except Exception:
            self.failure_count += 1
            if self.failure_count >= self.failure_threshold:
                self._state = "OPEN"
                self._opened_at = time.time()
            raise
        # 成功路径（CLOSED 常规成功 / HALF_OPEN 试探成功）→ 重置计数
        if self._state == "HALF_OPEN":
            self._state = "CLOSED"
        self.failure_count = 0
        self._opened_at = None
        return result

    def _recovery_elapsed(self) -> bool:
        return self._opened_at is not None and \
            (time.time() - self._opened_at) >= self.recovery_timeout


class Reranker:
    """Cross-Encoder 精排：逐对打分写回 doc.score，按分数降序返回 top_n（config reranker.top_n）

    生产链路熔断（I-1 终审）：构造即持有 RerankerCircuitBreaker，rerank 核心逻辑经
    breaker.call 执行 — predict 连续失败达到阈值 → 熔断打开，CircuitBreakerOpen 抛给
    上层（RetrievalService 捕获后降级为粗排结果截断）。
    """

    def __init__(self, model=None):
        # 延迟加载：model=None 时不加载模型（测试友好），首次 rerank 时按配置加载
        self.model = model
        self.top_n = ConfigRegistry.get("reranker.top_n", 5)
        # 熔断器：默认参数从 config reranker.circuit_breaker.* 读取（3 / 60），
        # 与 top_n 的读取时机一致（构造时 config 已 init）
        self.breaker = RerankerCircuitBreaker()
        # 模型加载互斥锁（R8）：评估并发 5 首触时，多个线程同时进入 _ensure_model
        # 各自加载 CrossEncoder 到 MPS → 设备初始化竞争，单次加载从 ~3.4s 爆炸到
        # ~47.8s（R4 实测 5 样本 rerank=47.8s 降级）。锁串行化加载：并发首触只加载
        # 一次，其余线程等待复用；稳态下无竞争，仅 ~100ns 开销。
        self._model_lock = threading.Lock()

    def _ensure_model(self):
        if self.model is None:
            with self._model_lock:
                # double-checked：拿到锁后再查一次，避免第二个线程重复加载
                if self.model is None:
                    # 延迟导入：避免测试/模块导入时加载 torch + sentence-transformers
                    from sentence_transformers import CrossEncoder
                    self.model = CrossEncoder(
                        ConfigRegistry.get("reranker.model"),
                        device=ConfigRegistry.get("reranker.device"),
                    )
        return self.model

    def ensure_loaded(self) -> None:
        """预热模型（首次加载耗时 >2s，须在阶段超时保护外调用）"""
        self._ensure_model()

    def rerank(self, query: str, candidates: List[ScoredDoc],
               top_n: Optional[int] = _RANK_DEFAULT) -> List[ScoredDoc]:
        """重排候选并写回 doc.score（精排分数），按分数降序返回。

        top_n 语义（P1h 双通道 RRF 二次融合需要全量位次，见 services/retrieval.py）：
        - 不传（默认）→ 截断到 config reranker.top_n（向后兼容现有调用方/测试）
        - 显式 top_n=None → 全量排序不截断（融合层从全排序中取每个 doc 的位次）
        - 显式 top_n=N（正整数）→ 截断到 N
        """
        if not candidates:
            return []
        if top_n is _RANK_DEFAULT:
            top_n = self.top_n
        # 核心逻辑经熔断器执行：predict 异常累计达阈值 → OPEN，下次调用抛 CircuitBreakerOpen
        return self.breaker.call(lambda: self._rerank_impl(query, candidates, top_n))

    def _rerank_impl(self, query: str, candidates: List[ScoredDoc],
                     top_n: Optional[int]) -> List[ScoredDoc]:
        model = self._ensure_model()
        # 输入截断（配置化，R15）：R7 验证 200 char 截断切掉答案信号——
        # gift register 答案 chunk 全文 rerank 第 3 名 vs 截断第 8 名；且"黄金信号
        # 在 chunk 开头"是未实测假设（铁律 6），条款式文档答案句常在 chunk 后段。
        # 历史性能约束：15 候选全文 2248ms 超 2s 预算 → 截断为配置项，默认 800。
        truncate = int(ConfigRegistry.get("reranker.input_truncate_chars", 0))
        # 0 = 全文不截断（A 方案：答案句深在 chunk 后段，任何短截断都会切掉答案信号——
        # 实测 gift register 答案 chunk 200 char 截断第14 vs 全文第1）
        pairs = [(query, doc.text[:truncate] if truncate > 0 else doc.text)
                 for doc in candidates]
        scores = model.predict(pairs)
        # predict 返回形状可能为 (n,) 或 (n, 1)，统一展平
        flat = scores.ravel() if hasattr(scores, "ravel") else list(scores)
        for doc, score in zip(candidates, flat):
            doc.score = float(score)  # 写回 rerank 分数
        ranked = sorted(candidates, key=lambda d: d.score, reverse=True)
        if top_n:
            ranked = ranked[:top_n]   # None → 全量不截断（P1h 融合取位次）
        return ranked
