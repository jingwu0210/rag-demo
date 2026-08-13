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
        # v1.6: doc_type 关键词层删除 → 任何分类输入都不产生 doc_type 过滤
        assert r.build_where("general") == {"is_active": True}
        assert r.build_where("handbook") == {"is_active": True}
        assert r.build_where("compliance") == {"is_active": True}


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

        # v1.6: doc_type 过滤删除 → 任何 filter 输入都返回全部 active chunks
        result = retriever.retrieve("年假", top_k=4, doc_type_filter="handbook")
        assert result.mode == "vector-only"
        assert {d.chunk_id for d in result.docs} == {"h1", "h2", "t1"}
        assert all(d.metadata["is_active"] is True for d in result.docs)

        # 无 filter → 同样返回全部 active chunks
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


def test_bm25_retriever_empty_collection_returns_empty():
    from core.retriever import BM25Retriever

    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)  # 空集合
        retriever = BM25Retriever(store)
        result = retriever.retrieve("年假", top_k=5)
        assert result.docs == []  # 不抛 ZeroDivisionError


def test_bm25_retriever_all_inactive_returns_empty():
    from core.retriever import BM25Retriever

    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        _add_chunks(store, [
            {"id": "del1", "text": "已废弃文档A", "metadata": {"doc_type": "handbook", "is_active": False}},
            {"id": "del2", "text": "已废弃文档B", "metadata": {"doc_type": "technical", "is_active": False}},
        ])
        retriever = BM25Retriever(store)
        retriever.build_index()
        result = retriever.retrieve("年假", top_k=5)
        assert result.docs == []


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


def test_hybrid_retriever_empty_collection_no_exception():
    from core.retriever import HybridRetriever

    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)  # 空集合
        retriever = HybridRetriever(store, _mock_embedder())
        result = retriever.retrieve("年假")
        assert result.mode == "hybrid"
        assert result.docs == []


def test_hybrid_retriever_empty_bm25_side_uses_vector_only():
    from core.retriever import HybridRetriever

    with tempfile.TemporaryDirectory() as tmp:
        store = _init_store(tmp)
        retriever = HybridRetriever(store, _mock_embedder())
        # 首次检索：空库 → BM25 索引建成空语料
        assert retriever.retrieve("年假").docs == []
        # 之后入库：vector 侧可查到，BM25 侧仍为空 → 仅 vector 结果参与 RRF
        _add_chunks(store, [
            {"id": "h1", "text": "员工年假政策说明", "metadata": {"doc_type": "handbook", "is_active": True}},
            {"id": "t1", "text": "API 技术规范文档", "metadata": {"doc_type": "technical", "is_active": True}},
        ])
        result = retriever.retrieve("年假")
        assert result.mode == "hybrid"
        assert result.docs  # 非空，不抛异常
        assert {d.chunk_id for d in result.docs} == {"h1", "t1"}
        assert all(d.score > 0 for d in result.docs)  # rrf 分数（仅 vector 一路贡献）


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


def test_adaptive_k_mode_aware_threshold():
    """R2: hybrid 模式用 hybrid_min_score（RRF 尺度），vector-only 用 min_score（余弦尺度）"""
    from core.config import ConfigRegistry
    from core.retriever import AdaptiveK, ScoredDoc

    ConfigRegistry.init("config.yaml")
    # RRF 尺度分数：0.0164（单路 rank1 理论值）及以上应保留
    rrf_docs = [
        ScoredDoc(chunk_id="a", text="t", score=0.0328),  # 两路 rank1
        ScoredDoc(chunk_id="b", text="t", score=0.0164),  # 单路 rank1（边界）
        ScoredDoc(chunk_id="c", text="t", score=0.0100),  # 单路靠后
        ScoredDoc(chunk_id="d", text="t", score=0.0050),  # 更弱
    ]
    kept = AdaptiveK().apply(rrf_docs, mode="hybrid")
    kept_ids = {d.chunk_id for d in kept}
    assert "a" in kept_ids and "b" in kept_ids      # 阈值之上/边界保留
    assert "c" not in kept_ids and "d" not in kept_ids  # 低于 RRF 尺度阈值被滤

    # vector-only 模式仍用余弦尺度 0.45
    vec_docs = [
        ScoredDoc(chunk_id="a", text="t", score=0.80),
        ScoredDoc(chunk_id="b", text="t", score=0.20),
    ]
    kept_vec = AdaptiveK().apply(vec_docs, mode="vector-only")
    assert [d.chunk_id for d in kept_vec] == ["a"]

    ConfigRegistry.override("retrieval.adaptive.hybrid_min_score", 0.0164)


def test_hybrid_retriever_vector_top1_sim_bypass():
    """R2: HybridRetriever 填充 vector_top1_sim 旁路字段（不参与排序，供 RefusalCheck）"""
    import tempfile
    import numpy as np
    from unittest.mock import MagicMock

    from core.config import ConfigRegistry
    from core.retriever import HybridRetriever
    from storage.chroma_client import ChromaStore

    with tempfile.TemporaryDirectory() as tmp:
        ConfigRegistry.init("config.yaml")
        ConfigRegistry.override("chromadb.persist_directory", tmp)
        store = ChromaStore()
        store.add(
            ids=["c1", "c2"],
            documents=["年假政策文本", "API 规范文本"],
            embeddings=[[0.9] * 16, [0.1] * 16],
            metadatas=[
                {"doc_type": "handbook", "is_active": True, "source_file_stem": "h"},
                {"doc_type": "technical", "is_active": True, "source_file_stem": "t"},
            ],
        )
        embedder = MagicMock()
        embedder.encode.return_value = np.array([[0.9] * 16])
        retriever = HybridRetriever(store, embedder)
        result = retriever.retrieve("年假", top_k=20)
        assert result.vector_top1_sim is not None
        assert 0.0 <= result.vector_top1_sim <= 1.0


def test_retriever_facade_skip_adaptive_for_rerank_pool():
    """R7: skip_adaptive=True 时 hybrid 粗排不截断，完整候选留给 reranker"""
    import tempfile
    import numpy as np
    from unittest.mock import MagicMock

    from core.config import ConfigRegistry
    from core.retriever import Retriever
    from storage.chroma_client import ChromaStore

    with tempfile.TemporaryDirectory() as tmp:
        ConfigRegistry.init("config.yaml")
        ConfigRegistry.override("chromadb.persist_directory", tmp)
        ConfigRegistry.override("retrieval.mode", "hybrid+rerank")
        store = ChromaStore()
        # 9 条文档：普通截断（max_chunks=8）和 skip 截断（全 9 条）可区分
        store.add(
            ids=[f"c{i}" for i in range(9)],
            documents=[f"文档内容编号 {i} 年假政策规定" for i in range(9)],
            embeddings=[[0.5] * 16 for _ in range(9)],
            metadatas=[{"doc_type": "handbook", "is_active": True,
                        "source_file_stem": f"doc{i}"} for i in range(9)],
        )
        embedder = MagicMock()
        embedder.encode.return_value = np.array([[0.5] * 16])
        retriever = Retriever(store, embedder)

        normal = retriever.retrieve("年假", top_k=20)
        assert len(normal.docs) <= 8  # AdaptiveK max_chunks 截断

        pooled = retriever.retrieve("年假", top_k=20, skip_adaptive=True)
        assert len(pooled.docs) > len(normal.docs)  # 候选池不截断，更多候选给 rerank
