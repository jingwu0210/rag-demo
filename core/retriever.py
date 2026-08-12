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


@dataclass
class RetrievalResult:
    docs: List[ScoredDoc]
    mode: str                    # "vector-only" | "hybrid" | "hybrid+rerank"
    timing_ms: int = 0


class AdaptiveK:
    """动态截断：min_score 过滤 + [min_chunks, max_chunks] 截断"""

    def __init__(self, min_score: Optional[float] = None,
                 min_chunks: Optional[int] = None,
                 max_chunks: Optional[int] = None):
        # 未显式传入时默认从 config retrieval.adaptive.* 读取
        self.enabled = ConfigRegistry.get("retrieval.adaptive.enabled", True)
        self.min_score = ConfigRegistry.get("retrieval.adaptive.min_score") if min_score is None else min_score
        self.min_chunks = ConfigRegistry.get("retrieval.adaptive.min_chunks") if min_chunks is None else min_chunks
        self.max_chunks = ConfigRegistry.get("retrieval.adaptive.max_chunks") if max_chunks is None else max_chunks

    def apply(self, docs: List[ScoredDoc]) -> List[ScoredDoc]:
        if not self.enabled or not docs:
            return docs
        # Step 1: 分数阈值过滤
        kept = [d for d in docs if self.min_score is None or d.score >= self.min_score]
        # Step 2: 全部低于阈值 → 保底返回前 min_chunks 条
        if not kept and self.min_chunks:
            kept = docs[: self.min_chunks]
        # Step 3: 超过 max_chunks → 截断
        if self.max_chunks:
            kept = kept[: self.max_chunks]
        return kept


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
                 doc_type_filter: Optional[str] = None) -> RetrievalResult:
        where = self.build_where(doc_type_filter)
        docs = self._to_scored_docs(self._search(query, top_k, where))
        docs = self.adaptive.apply(docs)
        return RetrievalResult(docs=docs, mode="vector-only")


class BM25Retriever(BaseRetriever):
    """in-memory BM25（rank_bm25 + jieba 分词），从 ChromaStore 的 active chunks 构建索引"""

    def __init__(self, chroma_store: ChromaStore):
        super().__init__(chroma_store)
        self._bm25 = None
        self._ids: List[str] = []
        self._texts: List[str] = []
        self._metadatas: List[Dict[str, Any]] = []

    def build_index(self) -> None:
        """拉全量 is_active chunks，tokenize，建 BM25Okapi"""
        results = self._store.collection.get(where={"is_active": True})
        self._ids = list(results["ids"] or [])
        self._texts = list(results["documents"] or [])
        self._metadatas = [m or {} for m in (results["metadatas"] or [])]
        tokenized = [jieba.lcut(t) for t in self._texts]
        self._bm25 = BM25Okapi(tokenized)

    def _search(self, query: str, top_k: int, doc_type_filter: Optional[str] = None) -> list:
        if self._bm25 is None:
            self.build_index()  # 惰性构建
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
    """VectorRetriever + BM25Retriever + RRFFusion(k=rrf_k)"""

    def __init__(self, chroma_store: ChromaStore, embedder: Embedder):
        super().__init__(chroma_store)
        self._vector = VectorRetriever(chroma_store, embedder)
        self._bm25 = BM25Retriever(chroma_store)
        self._rrf_k = ConfigRegistry.get("retrieval.fusion.rrf_k", 60)

    def retrieve(self, query: str, top_k: int = 20,
                 doc_type_filter: Optional[str] = None) -> RetrievalResult:
        vec_top_k = ConfigRegistry.get("retrieval.vector.top_k", top_k)
        bm25_top_k = ConfigRegistry.get("retrieval.bm25.top_k", top_k)
        where = self.build_where(doc_type_filter)
        vec_docs = self._vector._search(query, vec_top_k, where)
        bm25_docs = self._bm25._search(query, bm25_top_k, doc_type_filter)
        fused = RRFFusion.fuse(vec_docs, bm25_docs, k=self._rrf_k)
        scored = [ScoredDoc(chunk_id=d["chunk_id"], text=d["text"],
                            score=d["rrf_score"], metadata=d.get("metadata", {}))
                  for d in fused[:top_k]]
        scored = self.adaptive.apply(scored)
        return RetrievalResult(docs=scored, mode="hybrid")


class Retriever:
    """策略分发门面 — 按 config.retrieval.mode 选择实现，后续 Service 只依赖这个类"""

    def __init__(self, chroma_store: ChromaStore, embedder: Embedder):
        self._mode = ConfigRegistry.get("retrieval.mode", "hybrid+rerank")
        if self._mode == "vector-only":
            self._impl = VectorRetriever(chroma_store, embedder)
        elif self._mode in ("hybrid", "hybrid+rerank"):
            # rerank 由 RetrievalService 在下一层做，Retriever 只负责粗排
            self._impl = HybridRetriever(chroma_store, embedder)
        else:
            raise ValueError(f"unknown retrieval.mode: {self._mode}")

    def retrieve(self, query: str, top_k: int = 20,
                 doc_type_filter: Optional[str] = None) -> RetrievalResult:
        start = time.perf_counter()
        result = self._impl.retrieve(query, top_k, doc_type_filter)
        result.timing_ms = int((time.perf_counter() - start) * 1000)
        result.mode = self._mode  # 门面兜底：结果 mode 与配置一致（如 hybrid+rerank）
        return result
