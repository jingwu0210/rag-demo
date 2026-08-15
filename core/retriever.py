"""检索策略：Vector / BM25 / Hybrid 检索器 + AdaptiveK + Retriever 策略门面"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import jieba
from rank_bm25 import BM25Okapi

from core.config import ConfigRegistry
from core.fusion import RRFFusion
from core.metadata import ExpireFilter, MetadataFilter
from storage.chroma_client import ChromaStore

if TYPE_CHECKING:
    from core.embedder import Embedder


@dataclass
class ScoredDoc:
    chunk_id: str
    text: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    # P2c: 融合时携带的向量路余弦分（0-1，有绝对语义）；纯 BM25 命中为 None
    vec_sim: Optional[float] = None
    # P2d: BM25 路排名（1-based）；BM25 未命中为 None（强命中豁免过滤用）
    bm25_rank: Optional[int] = None


@dataclass
class RetrievalResult:
    docs: List[ScoredDoc]
    mode: str                    # "vector-only" | "hybrid" | "hybrid+rerank"
    timing_ms: int = 0
    # 旁路信号（R2 分数语义契约）：向量路 top1 余弦相似度（0-1，有绝对语义）。
    # RRF 分数是语料内相对排名，OOS 问题的 top1 RRF 照样高分 → 拦不住 OOS；
    # 此字段不参与排序，仅供 RefusalCheck 做置信度判定。
    vector_top1_sim: Optional[float] = None


class AdaptiveK:
    """动态截断：按检索模式区分分数语义（R2 分数语义契约）

    - vector-only: score = 余弦相似度（0-1）→ min_score 绝对阈值有意义
    - hybrid: score = RRF 排名融合分（~0.016-0.033，实现公式 1/(60+rank)）
      → min_score（余弦尺度）不可比，改用 hybrid_min_score（RRF 尺度，
      默认 = 单路 rank1 理论值 1/61 ≈ 0.0164，即"至少一路排第一"才保留）
    - hybrid+rerank: 粗排阶段由调用方 skip_adaptive 绕过本类（候选留给 rerank），
      最终 LLM 输入条数由 reranker top_n 决定
    """

    def __init__(self, min_score: Optional[float] = None,
                 min_chunks: Optional[int] = None,
                 max_chunks: Optional[int] = None):
        # 未显式传入时默认从 config retrieval.adaptive.* 读取
        self.enabled = ConfigRegistry.get("retrieval.adaptive.enabled", True)
        self.min_score = ConfigRegistry.get("retrieval.adaptive.min_score") if min_score is None else min_score
        self.hybrid_min_score = ConfigRegistry.get("retrieval.adaptive.hybrid_min_score", 0.0164)
        self.min_chunks = ConfigRegistry.get("retrieval.adaptive.min_chunks") if min_chunks is None else min_chunks
        self.max_chunks = ConfigRegistry.get("retrieval.adaptive.max_chunks") if max_chunks is None else max_chunks

    def apply(self, docs: List[ScoredDoc], mode: str = "vector-only") -> List[ScoredDoc]:
        if not self.enabled or not docs:
            return docs
        threshold = self.min_score if mode == "vector-only" else self.hybrid_min_score
        # Step 1: 分数阈值过滤（按模式选择阈值尺度）
        kept = [d for d in docs if threshold is None or d.score >= threshold]
        # Step 2: 全部低于阈值 → 保底返回前 min_chunks 条
        if not kept and self.min_chunks:
            kept = docs[: self.min_chunks]
        # Step 3: 超过 max_chunks → 截断
        if self.max_chunks:
            kept = kept[: self.max_chunks]
        return kept


def apply_vec_sim_filter(docs: List[ScoredDoc], threshold: Optional[float],
                         bm25_exempt_rank: int = 3) -> List[ScoredDoc]:
    """P2c+P2d: 融合后按 vec_sim 硬过滤（兜底，发生在 AdaptiveK 之前）

    纯 BM25 命中（vec_sim=None）视为 0 被滤 — 这正是过滤目标：
    BM25 独中但向量不相关的噪声。threshold=None 时不过滤（关闭过滤）。
    实测依据：R4 术语类 17 条（ISO 27001/429/SEV/VPN 等）答案依据 chunk
    与 query 余弦全部 ≥0.4781，0.45 阈值零误杀；普通中文样本全部 >0.45。

    P2d 豁免（R7 残余 #5）：口语短问句向量分崩（实测"公司年假有几天"
    top1=0.236）但 BM25 精确命中——bm25_rank ≤ bm25_exempt_rank 的强命中
    豁免过滤；rank 靠后的 BM25 噪声仍被拦截。
    """
    if threshold is None:
        return docs
    return [d for d in docs
            if (d.vec_sim if d.vec_sim is not None else 0.0) >= threshold
            or (getattr(d, "bm25_rank", None) is not None
                and d.bm25_rank <= bm25_exempt_rank)]


class BaseRetriever(ABC):
    """检索器基类：共用 where 子句构建与 ScoredDoc 转换"""

    def __init__(self, chroma_store: ChromaStore):
        self._store = chroma_store
        self.adaptive = AdaptiveK()

    def build_where(self, doc_type_filter: Optional[str]):
        """构建 chromadb where 子句：is_active + doc_type + expire 过滤"""
        conditions = [{"is_active": True}]
        if doc_type_filter:
            doc_types = MetadataFilter.get_doc_types(doc_type_filter)
            if doc_types:
                if len(doc_types) == 1:
                    conditions.append({"doc_type": doc_types[0]})
                else:
                    conditions.append({"$or": [{"doc_type": t} for t in doc_types]})
        expire_clause = ExpireFilter().get_where_clause()
        if expire_clause:
            conditions.append(expire_clause)
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}

    @staticmethod
    def _to_scored_docs(docs: list) -> List[ScoredDoc]:
        """chroma query 返回 dict（chunk_id/text/score/metadata）→ ScoredDoc"""
        return [ScoredDoc(**d) for d in docs]

    @abstractmethod
    def retrieve(self, query: str, top_k: int = 20,
                 doc_type_filter: Optional[str] = None) -> RetrievalResult:
        ...


class VectorRetriever(BaseRetriever):
    """纯向量检索：encode → chroma query → ScoredDoc → AdaptiveK"""

    def __init__(self, chroma_store: ChromaStore, embedder: Embedder):
        super().__init__(chroma_store)
        self._embedder = embedder

    def _search(self, query: str, top_k: int, where: Optional[dict]) -> list:
        vec = self._embedder.encode([query])[0]
        # chromadb 的 n_results 不能超过索引元素数（超出抛错）→ 收敛到全库条数
        n = max(1, min(top_k, self._store.collection.count()))
        return self._store.query(vec, n, where)

    def retrieve(self, query: str, top_k: int = 20,
                 doc_type_filter: Optional[str] = None,
                 skip_adaptive: bool = False) -> RetrievalResult:
        where = self.build_where(doc_type_filter)
        docs = self._to_scored_docs(self._search(query, top_k, where))
        vector_top1_sim = float(docs[0].score) if docs else None
        if not skip_adaptive:
            docs = self.adaptive.apply(docs, mode="vector-only")
        return RetrievalResult(docs=docs, mode="vector-only",
                               vector_top1_sim=vector_top1_sim)


class BM25Retriever(BaseRetriever):
    """in-memory BM25（rank_bm25 + jieba 分词），从 ChromaStore 的 active chunks 构建索引"""

    def __init__(self, chroma_store: ChromaStore):
        super().__init__(chroma_store)
        self._bm25 = None
        self._index_built = False
        self._ids: List[str] = []
        self._texts: List[str] = []
        self._metadatas: List[Dict[str, Any]] = []

    def build_index(self) -> None:
        """拉全量 is_active chunks，tokenize，建 BM25Okapi；空语料不建索引（BM25Okapi([]) 会 ZeroDivisionError）"""
        results = self._store.collection.get(where={"is_active": True})
        self._ids = list(results["ids"] or [])
        self._texts = list(results["documents"] or [])
        self._metadatas = [m or {} for m in (results["metadatas"] or [])]
        if self._texts:
            tokenized = [jieba.lcut(t) for t in self._texts]
            self._bm25 = BM25Okapi(tokenized)
        else:
            self._bm25 = None
        self._index_built = True

    def _search(self, query: str, top_k: int, doc_type_filter: Optional[str] = None) -> list:
        if not self._index_built:
            self.build_index()  # 惰性构建
        if not self._texts:
            # 无 active chunks → 与 vector 侧空库行为对齐：返回空结果，不抛异常
            return []
        tokens = jieba.lcut(query)
        scores = self._bm25.get_scores(tokens)
        allowed = None
        if doc_type_filter:
            doc_types = MetadataFilter.get_doc_types(doc_type_filter)
            allowed = set(doc_types) if doc_types else None  # general → 不过滤
        ranked = []
        for i, s in enumerate(scores):
            if allowed is not None and self._metadatas[i].get("doc_type") not in allowed:
                continue
            ranked.append((float(s), i))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [{
            "chunk_id": self._ids[i],
            "text": self._texts[i],
            "score": s,
            "metadata": self._metadatas[i],
        } for s, i in ranked[:top_k]]

    def retrieve(self, query: str, top_k: int = 20,
                 doc_type_filter: Optional[str] = None) -> RetrievalResult:
        # BM25 分数不是 0-1，min_score 不适用 → 不做 AdaptiveK，直接返回 top_k
        docs = self._to_scored_docs(self._search(query, top_k, doc_type_filter))
        return RetrievalResult(docs=docs, mode="bm25")


class HybridRetriever(BaseRetriever):
    """VectorRetriever + BM25Retriever + 加权 RRFFusion + vec_sim 硬过滤（P2）"""

    def __init__(self, chroma_store: ChromaStore, embedder: Embedder):
        super().__init__(chroma_store)
        self._vector = VectorRetriever(chroma_store, embedder)
        self._bm25 = BM25Retriever(chroma_store)

    def retrieve(self, query: str, top_k: int = 20,
                 doc_type_filter: Optional[str] = None,
                 skip_adaptive: bool = False) -> RetrievalResult:
        # P2 融合参数每次 retrieve 动态读取（与门面"改配置 = 改行为"语义一致）
        rrf_k = ConfigRegistry.get("retrieval.fusion.rrf_k", 60)
        vector_weight = ConfigRegistry.get("retrieval.fusion.vector_weight", 1.0)
        bm25_weight = ConfigRegistry.get("retrieval.fusion.bm25_weight", 0.8)
        vec_sim_threshold = ConfigRegistry.get(
            "retrieval.fusion.vector_sim_threshold", 0.45)
        vec_top_k = ConfigRegistry.get("retrieval.vector.top_k", top_k)
        bm25_top_k = ConfigRegistry.get("retrieval.bm25.top_k", top_k)
        where = self.build_where(doc_type_filter)
        vec_docs = self._vector._search(query, vec_top_k, where)
        bm25_docs = self._bm25._search(query, bm25_top_k, doc_type_filter)
        fused = RRFFusion.fuse(vec_docs, bm25_docs, k=rrf_k,
                               vector_weight=vector_weight,
                               bm25_weight=bm25_weight)
        scored = [ScoredDoc(chunk_id=d["chunk_id"], text=d["text"],
                            score=d["rrf_score"], metadata=d.get("metadata", {}),
                            vec_sim=d.get("vec_sim"), bm25_rank=d.get("bm25_rank"))
                  for d in fused]
        # P2c+P2d: 融合后 vec_sim 硬过滤（发生在 AdaptiveK 之前；skip_adaptive
        # 的 rerank 粗排池走同一路径，候选池同样更干净）；BM25 强命中豁免
        bm25_exempt_rank = ConfigRegistry.get(
            "retrieval.fusion.bm25_exempt_rank", 3)
        scored = apply_vec_sim_filter(
            scored, vec_sim_threshold, bm25_exempt_rank)[:top_k]
        # R2 旁路信号：向量路 top1 余弦分数（0-1），仅供 RefusalCheck 置信度判定。
        # 语义不变（P2c 过滤不改变它：过滤阈值与置信度阈值同为 0.45，
        # 过滤后非空 ⟺ 向量路 top1 ≥ 0.45）
        vector_top1_sim = (float(vec_docs[0]["score"]) if vec_docs else None)
        if not skip_adaptive:
            # hybrid: RRF 尺度阈值（hybrid_min_score），非余弦尺度 min_score
            scored = self.adaptive.apply(scored, mode="hybrid")
        return RetrievalResult(docs=scored, mode="hybrid",
                               vector_top1_sim=vector_top1_sim)


class Retriever:
    """策略分发门面 — 按 config.retrieval.mode 选择实现，后续 Service 只依赖这个类

    mode 在每次 retrieve 时动态读取（不缓存）：改配置 = 改行为，已构造实例可即时切换
    （eval 三配置对比依赖此语义）。子检索器按 mode 懒加载并缓存，切回同一 mode 不重复构造。
    """

    def __init__(self, chroma_store: ChromaStore, embedder: Embedder):
        self._chroma_store = chroma_store
        self._embedder = embedder
        self._impls: Dict[str, BaseRetriever] = {}

    def _get_impl(self, mode: str) -> BaseRetriever:
        # hybrid 与 hybrid+rerank 的粗排实现相同（rerank 由 RetrievalService 在下一层做）→ 共享实例
        impl_key = "hybrid" if mode in ("hybrid", "hybrid+rerank") else mode
        if impl_key not in self._impls:
            if mode == "vector-only":
                self._impls[impl_key] = VectorRetriever(self._chroma_store, self._embedder)
            elif mode in ("hybrid", "hybrid+rerank"):
                self._impls[impl_key] = HybridRetriever(self._chroma_store, self._embedder)
            else:
                raise ValueError(f"unknown retrieval.mode: {mode}")
        return self._impls[impl_key]

    def retrieve(self, query: str, top_k: int = 20,
                 doc_type_filter: Optional[str] = None,
                 skip_adaptive: bool = False,
                 mode: Optional[str] = None) -> RetrievalResult:
        """mode=None → config 全局值（eval 三配置切换依赖）；否则请求级覆盖（/chat 透传）。"""
        start = time.perf_counter()
        mode = mode or ConfigRegistry.get("retrieval.mode", "hybrid+rerank")
        impl = self._get_impl(mode)
        if skip_adaptive and isinstance(impl, HybridRetriever):
            # R7: rerank 候选池独立 — 粗排结果不截断，完整候选留给 reranker 精排
            result = impl.retrieve(query, top_k, doc_type_filter, skip_adaptive=True)
        else:
            result = impl.retrieve(query, top_k, doc_type_filter)
        result.timing_ms = int((time.perf_counter() - start) * 1000)
        result.mode = mode  # 门面兜底：结果 mode 与当前配置一致（如 hybrid+rerank）
        return result
