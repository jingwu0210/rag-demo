"""检索子链路测试：RRF 融合 + AdaptiveK + Vector/BM25/Hybrid 检索器 + Retriever 门面"""
import tempfile
from unittest.mock import MagicMock

import numpy as np

from core.config import ConfigRegistry
from storage.chroma_client import ChromaStore


def _init_store(tmpdir):
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("chromadb.persist_directory", tmpdir)
    return ChromaStore()


def _mock_embedder(vec_len: int = 16) -> MagicMock:
    """mock Embedder：返回固定向量，维度一致即可（BGE-M3 太重，测试不加载）"""
    embedder = MagicMock()
    embedder.encode.return_value = np.array([[0.5] * vec_len])
    return embedder


def _add_chunks(store, chunks):
    store.add(
        ids=[c["id"] for c in chunks],
        documents=[c["text"] for c in chunks],
        embeddings=[[0.5] * 16 for _ in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )


# ── 1. RRF 融合 ──────────────────────────────────────────────

def test_rrf_fusion_ranks_common_docs_first():
    from core.fusion import RRFFusion

    vec = [
        {"chunk_id": "a", "text": "A", "score": 0.9},
        {"chunk_id": "b", "text": "B", "score": 0.8},
        {"chunk_id": "c", "text": "C", "score": 0.7},
    ]
    bm25 = [
        {"chunk_id": "b", "text": "B", "score": 10.0},
        {"chunk_id": "c", "text": "C", "score": 9.0},
    ]
    fused = RRFFusion.fuse(vec, bm25, k=60)
    # 两个列表都在前面的排最前；rrf 分数 = Σ 1/(k+rank+1)，rank 从 0 计
    assert [d["chunk_id"] for d in fused] == ["b", "c", "a"]
    # b: vec rank1 + bm25 rank0 = 1/62 + 1/61
    assert abs(fused[0]["rrf_score"] - (1 / 62 + 1 / 61)) < 1e-9
    # c: vec rank2 + bm25 rank1 = 1/63 + 1/62
    assert abs(fused[1]["rrf_score"] - (1 / 63 + 1 / 62)) < 1e-9
    # a: 仅 vec rank0 = 1/61
    assert abs(fused[2]["rrf_score"] - 1 / 61) < 1e-9
    # 只附加 rrf_score 字段，不改写原始字段
    assert fused[0]["text"] == "B"


# ── 2. AdaptiveK ─────────────────────────────────────────────

def test_adaptive_k_min_score_filter():
    from core.retriever import AdaptiveK, ScoredDoc

    ConfigRegistry.init("config.yaml")  # AdaptiveK 构造时读取 config retrieval.adaptive.*
    docs = [ScoredDoc(chunk_id=f"c{i}", text="", score=s)
            for i, s in enumerate([0.9, 0.8, 0.7, 0.5, 0.4, 0.3])]
    kept = AdaptiveK(min_score=0.45, min_chunks=3, max_chunks=8).apply(docs)
    assert [d.chunk_id for d in kept] == ["c0", "c1", "c2", "c3"]


def test_adaptive_k_fallback_when_all_below_min_score():
    from core.retriever import AdaptiveK, ScoredDoc

    ConfigRegistry.init("config.yaml")
    docs = [ScoredDoc(chunk_id=f"c{i}", text="", score=s)
            for i, s in enumerate([0.44, 0.4, 0.3, 0.2])]
    kept = AdaptiveK(min_score=0.45, min_chunks=3, max_chunks=8).apply(docs)
    assert len(kept) == 3  # 全部低于阈值 → 保底返回前 min_chunks 条
    assert [d.chunk_id for d in kept] == ["c0", "c1", "c2"]


def test_adaptive_k_max_chunks_truncation():
    from core.retriever import AdaptiveK, ScoredDoc

    ConfigRegistry.init("config.yaml")
    docs = [ScoredDoc(chunk_id=f"c{i}", text="", score=0.9 - i * 0.05) for i in range(10)]
    kept = AdaptiveK(min_score=0.3, min_chunks=3, max_chunks=4).apply(docs)
    assert len(kept) == 4


# ── where 子句构建（$and 规范）───────────────────────────────

def test_build_where_and_clause():
    from core.retriever import VectorRetriever

    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        r = VectorRetriever(store, _mock_embedder())
        assert r.build_where(None) == {"is_active": True}
        assert r.build_where("general") == {"is_active": True}
        assert r.build_where("handbook") == {
            "$and": [{"is_active": True}, {"doc_type": "handbook"}]}
        # compliance 映射多个 doc_type → $or
        assert r.build_where("compliance") == {
            "$and": [{"is_active": True}, {"$or": [{"doc_type": "compliance"}, {"doc_type": "handbook"}]}]}


# ── 3. VectorRetriever ───────────────────────────────────────

def test_vector_retriever_doc_type_filter_and_is_active():
    from core.retriever import VectorRetriever

    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _add_chunks(store, [
            {"id": "h1", "text": "员工年假政策说明", "metadata": {"doc_type": "handbook", "is_active": True}},
            {"id": "h2", "text": "加班与工资计算规则", "metadata": {"doc_type": "handbook", "is_active": True}},
            {"id": "t1", "text": "API 技术规范文档", "metadata": {"doc_type": "technical", "is_active": True}},
            {"id": "del", "text": "已废弃文档", "metadata": {"doc_type": "handbook", "is_active": False}},
        ])
        retriever = VectorRetriever(store, _mock_embedder())

        # doc_type_filter="handbook" → 只返回 handbook 且排除 inactive
        result = retriever.retrieve("年假", top_k=4, doc_type_filter="handbook")
        assert result.mode == "vector-only"
        assert {d.chunk_id for d in result.docs} == {"h1", "h2"}
        assert all(d.metadata["is_active"] is True for d in result.docs)

        # 无 filter → 返回全部 active chunks
        all_result = retriever.retrieve("年假", top_k=4)
        assert {d.chunk_id for d in all_result.docs} == {"h1", "h2", "t1"}


# ── 4. BM25Retriever ─────────────────────────────────────────

def test_bm25_retriever_hits_chinese_query():
    from core.retriever import BM25Retriever

    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _add_chunks(store, [
            {"id": "c1", "text": "员工年假政策：入职满一年可享受五天带薪年假",
             "metadata": {"doc_type": "handbook", "is_active": True}},
            {"id": "c2", "text": "API 接口调用规范与限流策略",
             "metadata": {"doc_type": "technical", "is_active": True}},
        ])
        retriever = BM25Retriever(store)
        retriever.build_index()
        result = retriever.retrieve("年假", top_k=5)
        assert result.docs
        assert result.docs[0].chunk_id == "c1"
        assert "年假" in result.docs[0].text


def test_bm25_retriever_lazy_index_build():
    from core.retriever import BM25Retriever

    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _add_chunks(store, [
            {"id": "c1", "text": "员工年假政策说明", "metadata": {"doc_type": "handbook", "is_active": True}},
        ])
        retriever = BM25Retriever(store)
        result = retriever.retrieve("年假", top_k=5)  # 不显式 build_index
        assert result.docs[0].chunk_id == "c1"


# ── 5. HybridRetriever ───────────────────────────────────────

def test_hybrid_retriever_rrf_ordering():
    from core.retriever import HybridRetriever

    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _add_chunks(store, [
            {"id": "c1", "text": "员工年假政策：入职满一年可享受五天带薪年假",
             "metadata": {"doc_type": "handbook", "is_active": True}},
            {"id": "c2", "text": "API 接口调用规范与限流策略",
             "metadata": {"doc_type": "technical", "is_active": True}},
        ])
        retriever = HybridRetriever(store, _mock_embedder())
        result = retriever.retrieve("年假")
        assert result.mode == "hybrid"
        assert result.docs
        # 双路都命中的 c1（rrf 分数更高）排最前
        assert result.docs[0].chunk_id == "c1"
        # rrf_score 作为 score，降序排列
        scores = [d.score for d in result.docs]
        assert scores == sorted(scores, reverse=True)
        assert all(s > 0 for s in scores)


# ── 6. Retriever 门面 ────────────────────────────────────────

def test_retriever_facade_mode_mapping():
    from core.retriever import Retriever

    for mode in ("vector-only", "hybrid", "hybrid+rerank"):
        with tempfile.TemporaryDirectory() as tmp:
            store = _init_store(tmp)
            _add_chunks(store, [
                {"id": "c1", "text": "员工年假政策说明", "metadata": {"doc_type": "handbook", "is_active": True}},
            ])
            ConfigRegistry.override("retrieval.mode", mode)
            retriever = Retriever(store, _mock_embedder())
            result = retriever.retrieve("年假", top_k=2)
            assert result.mode == mode
            assert result.docs
            assert 0 <= result.timing_ms < 10000
