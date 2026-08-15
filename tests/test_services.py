"""Task 7: Service Layer 测试 — RetrievalService / ChatService / IngestService

全部用 mock 下层组件（不加载 BGE 模型、不调 LLM、不依赖真实数据）：
- RetrievalService：mock retriever/reranker/scanner，验证重排 / 熔断降级 / 超时降级 / 注入阻断
- ChatService：全 mock 链路 + tempfile SQLite 真实验证（turns / request_metrics / sessions）
- IngestService：mock OCR/Chunker/Embedder/Versioned，验证调用顺序与 BM25 索引重建
- 异步测试用 asyncio.run 包裹（项目未装 pytest-asyncio）
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.cache import CachedAnswer
from core.chunker import Chunk
from core.config import ConfigRegistry
from core.generator import GenerationResult
from core.guard import ResilienceGuard, StageTimeoutError
from core.ocr import ParsedDoc
from core.postprocess import PostProcessResult
from core.retriever import RetrievalResult, ScoredDoc
from core.reranker import CircuitBreakerOpen
from core.versioned import IngestResult
from storage.sqlite_client import get_db, init_db

from services.chat import ChatResponse, ChatService
from services.ingest import IngestService
from services.retrieval import RetrievalOutput, RetrievalService


# ═══ helpers ═════════════════════════════════════════════════

def _scored_doc(cid: str, text: str, score: float,
                heading: str = "员工手册 > 第三章") -> ScoredDoc:
    return ScoredDoc(chunk_id=cid, text=text, score=score,
                     metadata={"heading_path": heading, "doc_type": "handbook"})


async def _fetch_turns(session_id: str):
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT turn_index, raw_query, answer, query_language, timing_json, "
            "sources_json, token_total, retrieval_mode, refused, from_cache "
            "FROM turns WHERE session_id = ? ORDER BY turn_index", (session_id,))
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _fetch_metrics():
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM request_metrics ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _fetch_sessions():
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM sessions ORDER BY created_at")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _fetch_ingest_logs():
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM ingest_log ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


# ═══ 1. RetrievalService ════════════════════════════════════

def _retrieval_mocks(docs, rerank_result=None, rerank_exc=None, scan_result=None):
    """构建 mock 下层组件：retriever / reranker / scanner"""
    retriever = MagicMock()
    retriever.retrieve = AsyncMock(
        return_value=RetrievalResult(docs=list(docs), mode="hybrid+rerank", timing_ms=15))
    reranker = MagicMock()
    if rerank_exc is not None:
        reranker.rerank = AsyncMock(side_effect=rerank_exc)
    else:
        reranker.rerank = AsyncMock(return_value=rerank_result)
    scanner = MagicMock()
    scanner.scan = MagicMock(return_value=scan_result or (list(docs), 0))
    return retriever, reranker, scanner


def test_retrieval_service_rerank_reorders_docs():
    """rerank 重排 → docs 顺序变化 + mode 正确 + injection_blocked 传递"""
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("reranker.enabled", True)
    docs = [_scored_doc(f"c{i}", f"内容 {i}", 0.9 - i * 0.1) for i in range(3)]
    retriever, reranker, scanner = _retrieval_mocks(
        docs,
        rerank_result=[docs[2], docs[0], docs[1]],   # 重排：c2 最相关
        scan_result=([docs[2], docs[0]], 1))         # 注入扫描 block 1 个
    svc = RetrievalService(retriever=retriever, reranker=reranker,
                           scanner=scanner, guard=ResilienceGuard())

    out = asyncio.run(svc.retrieve("年假有几天", doc_type="handbook"))

    assert isinstance(out, RetrievalOutput)
    assert out.mode == "hybrid+rerank"
    assert [d.chunk_id for d in out.docs] == ["c2", "c0"]
    assert out.injection_blocked == 1
    assert out.degraded is False
    assert set(out.timing_ms) == {"retrieval", "rerank", "total"}
    # 调用点按函数签名传位置参数（run_in_executor 仅支持位置参数）
    retriever.retrieve.assert_awaited_with(
        "年假有几天", 30, "handbook", True, "hybrid+rerank")  # candidates_multiplier=1.5
    reranker.rerank.assert_awaited_once()
    scanner.scan.assert_called_once()


def test_retrieval_service_dual_channel_rrf_fusion():
    """P1h: rerank 后双通道 RRF 二次融合 — 精排不独裁（R4 排序失真修复）

    粗排 [A,B,C]；CrossEncoder 全量排序 [C,A,B]（C 被顶到第一）。
    纯 rerank 结果应为 [C,A,B]；融合后 final = 1/(60+粗排位次) + 1/(60+rerank位次)
    （1-based rank，与 core/fusion.py 一致）：
    - A: 1/61 + 1/62 ≈ 0.0325224（粗排#1 + rerank#2）
    - C: 1/63 + 1/61 ≈ 0.0322665（粗排#3 + rerank#1）
    - B: 1/62 + 1/63 ≈ 0.0320020（粗排#2 + rerank#3）
    最终顺序 [A, C, B] — A 因粗排第一回归首位，C 居第二（未被精排独裁）。
    """
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("reranker.enabled", True)
    ConfigRegistry.override("reranker.top_n", 5)
    docs = [_scored_doc("A", "内容 A", 0.9), _scored_doc("B", "内容 B", 0.8),
            _scored_doc("C", "内容 C", 0.7)]
    retriever, reranker, scanner = _retrieval_mocks(
        docs, rerank_result=[docs[2], docs[0], docs[1]])   # mock 返回全排序 [C,A,B]
    scanner.scan = MagicMock(side_effect=lambda d: (d, 0))  # 透传融合后结果
    svc = RetrievalService(retriever=retriever, reranker=reranker,
                           scanner=scanner, guard=ResilienceGuard())

    out = asyncio.run(svc.retrieve("员工行为规范相关"))

    assert out.degraded is False
    assert [d.chunk_id for d in out.docs] == ["A", "C", "B"]
    k = int(ConfigRegistry.get("retrieval.fusion.rrf_k", 60))   # config 单源（与粗排融合共用）
    scores = {d.chunk_id: d.score for d in out.docs}
    assert scores["A"] == pytest.approx(1 / (k + 1) + 1 / (k + 2))
    assert scores["C"] == pytest.approx(1 / (k + 3) + 1 / (k + 1))
    assert scores["B"] == pytest.approx(1 / (k + 2) + 1 / (k + 3))
    reranker.rerank.assert_awaited_once()


def test_retrieval_service_rerank_circuit_breaker_degrades():
    """reranker 抛 CircuitBreakerOpen → degraded=True 且不抛异常，截断 top_n 兜底"""
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("reranker.enabled", True)
    ConfigRegistry.override("reranker.top_n", 2)
    docs = [_scored_doc(f"c{i}", f"内容 {i}", 0.9 - i * 0.1) for i in range(5)]
    retriever, reranker, scanner = _retrieval_mocks(
        docs, rerank_exc=CircuitBreakerOpen("Reranker 熔断中, 使用 hybrid 降级"))
    scanner.scan = MagicMock(side_effect=lambda d: (d, 0))   # 透传截断后的候选
    svc = RetrievalService(retriever=retriever, reranker=reranker,
                           scanner=scanner, guard=ResilienceGuard())

    out = asyncio.run(svc.retrieve("年假有几天"))

    assert out.degraded is True
    assert len(out.docs) == 2                      # 粗排结果截断 top_n 兜底
    assert out.docs[0].chunk_id == "c0"
    assert out.mode == "hybrid+rerank"


def test_retrieval_service_rerank_stage_timeout_degrades():
    """rerank 阶段超时 → StageTimeoutError 被捕获 → degraded=True"""
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("reranker.enabled", True)
    ConfigRegistry.override("reranker.timeout", 0.05)
    docs = [_scored_doc(f"c{i}", f"内容 {i}", 0.9) for i in range(3)]
    retriever, reranker, scanner = _retrieval_mocks(docs)

    async def slow_rerank(query, candidates):
        await asyncio.sleep(0.5)
        return candidates

    reranker.rerank = slow_rerank
    svc = RetrievalService(retriever=retriever, reranker=reranker,
                           scanner=scanner, guard=ResilienceGuard())

    out = asyncio.run(svc.retrieve("年假有几天"))

    assert out.degraded is True
    assert len(out.docs) == 3                      # top_n 默认 5 > 候选数，全保留


def test_retrieval_service_retrieval_timeout_empty_docs():
    """检索阶段超时 → 空 docs 继续（触发后续低置信拒答），mode 用配置兜底"""
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("retrieval.timeout", 0.05)
    retriever = MagicMock()

    async def slow_retrieve(query, top_k=20, doc_type_filter=None, skip_adaptive=False, mode=None):
        await asyncio.sleep(0.5)
        return RetrievalResult(docs=[_scored_doc("c1", "t", 0.9)], mode="hybrid")

    retriever.retrieve = slow_retrieve
    reranker = MagicMock()
    reranker.rerank = AsyncMock()
    scanner = MagicMock()
    scanner.scan = MagicMock(return_value=([], 0))
    svc = RetrievalService(retriever=retriever, reranker=reranker,
                           scanner=scanner, guard=ResilienceGuard())

    out = asyncio.run(svc.retrieve("年假有几天"))

    assert out.docs == []
    assert out.mode == ConfigRegistry.get("retrieval.mode")   # 配置兜底
    assert out.injection_blocked == 0
    reranker.rerank.assert_not_awaited()           # 无候选 → 跳过精排


def test_retrieval_service_rerank_disabled():
    """reranker.enabled=false → 跳过精排，mode 保留粗排结果"""
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("reranker.enabled", False)
    docs = [_scored_doc("c1", "内容", 0.9)]
    retriever = MagicMock()
    retriever.retrieve = AsyncMock(
        return_value=RetrievalResult(docs=list(docs), mode="hybrid"))
    reranker = MagicMock()
    reranker.rerank = AsyncMock()
    scanner = MagicMock()
    scanner.scan = MagicMock(return_value=(list(docs), 0))
    svc = RetrievalService(retriever=retriever, reranker=reranker,
                           scanner=scanner, guard=ResilienceGuard())

    out = asyncio.run(svc.retrieve("年假有几天"))

    assert out.mode == "hybrid"
    assert out.docs[0].chunk_id == "c1"
    reranker.rerank.assert_not_awaited()


def test_stage_timeout_reaches_sync_retrieval():
    """I-1 精确回归：同步阻塞检索必须能被阶段超时打断（时间上可达）"""
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("retrieval.timeout", 0.1)

    def slow_retrieve(query, top_k=20, doc_type_filter=None, skip_adaptive=False, mode=None):
        time.sleep(2)   # 同步阻塞 2s
        return RetrievalResult(docs=[], mode="vector-only")

    retriever = MagicMock()
    retriever.retrieve.side_effect = slow_retrieve
    svc = RetrievalService(retriever=retriever, guard=ResilienceGuard())

    start = time.perf_counter()
    result = asyncio.run(svc.retrieve("test", None))
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"超时未生效: {elapsed:.2f}s（若 >1.5s 说明修复失败）"
    assert result.docs == []          # 超时降级为空结果


def test_event_loop_responsive_during_blocked_retrieval():
    """I-1 并发影响回归：阻塞检索期间事件循环仍自由（心跳可继续跳动）"""
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("retrieval.timeout", 0.15)   # 心跳窗口 0.05s×2 < 超时，避免边界竞争

    def slow_retrieve(query, top_k=20, doc_type_filter=None, skip_adaptive=False, mode=None):
        time.sleep(2)
        return RetrievalResult(docs=[], mode="vector-only")

    async def scenario():
        retriever = MagicMock()
        retriever.retrieve.side_effect = slow_retrieve
        svc = RetrievalService(retriever=retriever, guard=ResilienceGuard())

        ticks = []

        async def heartbeat():
            while True:
                await asyncio.sleep(0.05)
                ticks.append(time.perf_counter())

        hb = asyncio.create_task(heartbeat())
        await svc.retrieve("test", None)   # 内部 0.15s 超时返回
        hb.cancel()
        return ticks

    ticks = asyncio.run(scenario())
    assert len(ticks) >= 2, f"事件循环被阻塞: 心跳仅 {len(ticks)} 次"


# ═══ 2. ChatService ═════════════════════════════════════════

def _chat_svc(tmp_path, *, cache_hit=False, cache_enabled=True, gen_exc=None,
              pp_result=None, retrieval_output=None):
    """构建全 mock ChatService + tempfile SQLite（含建表）"""
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("paths.sqlite", str(tmp_path / "chat.db"))
    ConfigRegistry.override("cache.l1.enabled", cache_enabled)
    asyncio.run(init_db())

    if retrieval_output is None:
        docs = [_scored_doc("c1", "年假规定", 0.91), _scored_doc("c2", "病假规定", 0.82)]
        retrieval_output = RetrievalOutput(
            docs=docs, mode="hybrid+rerank",
            timing_ms={"retrieval": 12, "rerank": 8, "total": 20},
            degraded=False, injection_blocked=0)
    retrieval = MagicMock()
    retrieval.retrieve = AsyncMock(return_value=retrieval_output)

    generator = MagicMock()
    if gen_exc is not None:
        generator.generate = AsyncMock(side_effect=gen_exc)
    else:
        generator.generate = AsyncMock(return_value=GenerationResult(
            text="根据《员工手册》规定，年假为 10 天。",
            token_prompt=10, token_completion=20, token_total=30, latency_ms=50))

    postprocessor = MagicMock()
    if pp_result is None:
        pp_result = PostProcessResult(answer="根据《员工手册》规定，年假为 10 天。")
    postprocessor.process = MagicMock(return_value=pp_result)

    cache = MagicMock()
    if cache_hit:
        cache.get = AsyncMock(return_value=CachedAnswer(
            answer="（缓存）每年 10 天",
            sources=[{"chunk_id": "c9", "heading_path": "员工手册", "score": 0.95}],
            token_usage=42, retrieval_mode="hybrid+rerank"))
    else:
        cache.get = AsyncMock(return_value=None)
    cache.put = AsyncMock()

    compressor = MagicMock()
    svc = ChatService(retrieval=retrieval, generator=generator,
                      postprocessor=postprocessor, cache=cache,
                      guard=ResilienceGuard(), compressor=compressor)
    return svc, {"retrieval": retrieval, "generator": generator,
                 "postprocessor": postprocessor, "cache": cache}


def test_chat_service_full_flow(tmp_path):
    """全 mock 链路：缓存未命中 → 检索 2 docs → 生成 → 后处理 → 完整 ChatResponse + 落库"""
    svc, deps = _chat_svc(tmp_path)

    resp = asyncio.run(svc.process("年假有几天？"))

    assert isinstance(resp, ChatResponse)
    assert resp.answer == "根据《员工手册》规定，年假为 10 天。"
    assert resp.session_id
    assert resp.sources == [
        {"chunk_id": "c1", "heading_path": "员工手册 > 第三章", "score": 0.91,
         "text": "年假规定"},    # I-4：sources 携带 chunk 文本（前 500 字符）
        {"chunk_id": "c2", "heading_path": "员工手册 > 第三章", "score": 0.82,
         "text": "病假规定"},
    ]
    assert resp.timing_ms["retrieval"] == 12
    assert resp.timing_ms["rerank"] == 8
    assert resp.timing_ms["generation"] == 50
    assert resp.timing_ms["total"] >= 0
    assert resp.token_usage == {"prompt": 10, "completion": 20, "total": 30}
    assert resp.refused is False and resp.from_cache is False and resp.partial is False
    # v1.6: classify 恒 general；mode=None → 回落到 config 全局值
    deps["retrieval"].retrieve.assert_awaited_with("年假有几天？", "general", "hybrid+rerank")
    deps["cache"].put.assert_awaited_once()

    # ── SQLite 持久化真实验证（tempfile）──
    turns = asyncio.run(_fetch_turns(resp.session_id))
    assert len(turns) == 1
    t = turns[0]
    assert t["turn_index"] == 0
    assert t["raw_query"] == "年假有几天？"
    assert t["query_language"] == "zh"
    assert json.loads(t["timing_json"])["retrieval"] == 12
    assert json.loads(t["sources_json"]) == resp.sources
    assert t["token_total"] == 30
    assert t["retrieval_mode"] == "hybrid+rerank"
    assert t["refused"] == 0

    metrics = asyncio.run(_fetch_metrics())
    assert len(metrics) == 1
    m = metrics[0]
    assert m["retrieval_mode"] == "hybrid+rerank"
    assert m["cache_hit"] == 0
    assert m["token_total"] == 30
    assert m["latency_retrieval"] == 12
    assert m["latency_generation"] == 50
    assert m["refused"] == 0 and m["timeout"] == 0 and m["degraded"] == 0
    assert m["pii_redact_count"] == 0 and m["injection_blocked"] == 0
    assert m["source"] == "chat"                  # 默认调用方 = chat 对话

    sessions = asyncio.run(_fetch_sessions())
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == resp.session_id
    assert sessions[0]["turn_count"] == 1


def test_chat_service_cache_hit(tmp_path):
    """缓存命中：from_cache=True，不调 retrieval/generator"""
    svc, deps = _chat_svc(tmp_path, cache_hit=True)

    resp = asyncio.run(svc.process("年假有几天？", session_id="s1"))

    assert resp.from_cache is True
    assert resp.answer == "（缓存）每年 10 天"
    assert resp.session_id == "s1"
    assert resp.sources == [{"chunk_id": "c9", "heading_path": "员工手册", "score": 0.95}]
    assert resp.token_usage["total"] == 42
    deps["retrieval"].retrieve.assert_not_awaited()
    deps["generator"].generate.assert_not_awaited()
    deps["cache"].put.assert_not_awaited()


def test_chat_service_cache_hit_writes_metrics(tmp_path):
    """I-2 终审：缓存命中路径也写 request_metrics（cache_hit=1），命中率才可计算"""
    svc, deps = _chat_svc(tmp_path, cache_hit=True)

    resp = asyncio.run(svc.process("年假有几天？", session_id="s1"))

    assert resp.from_cache is True
    metrics = asyncio.run(_fetch_metrics())
    assert len(metrics) == 1
    m = metrics[0]
    assert m["cache_hit"] == 1
    assert m["retrieval_mode"] == "hybrid+rerank"   # 记当前检索模式
    assert m["token_total"] == 42                   # 缓存时记录的 token_usage
    assert m["latency_total"] >= 0                  # 实际命中耗时
    assert m["latency_retrieval"] == 0 and m["latency_generation"] == 0
    assert m["refused"] == 0 and m["timeout"] == 0 and m["degraded"] == 0
    assert m["source"] == "chat"                    # 缓存命中路径同样带 source


def test_chat_service_source_eval(tmp_path):
    """source="eval"（评估跑批）→ request_metrics.source='eval'，与 chat 对话区分"""
    svc, deps = _chat_svc(tmp_path)

    resp = asyncio.run(svc.process("年假有几天？", None, source="eval"))

    assert resp.answer == "根据《员工手册》规定，年假为 10 天。"
    metrics = asyncio.run(_fetch_metrics())
    assert len(metrics) == 1
    assert metrics[0]["source"] == "eval"


def test_chat_service_generation_timeout_partial(tmp_path):
    """生成超时：partial=True + 降级话术；不写缓存；仍落库 turn/metrics（timeout=True）"""
    svc, deps = _chat_svc(tmp_path, gen_exc=StageTimeoutError("generation"))

    resp = asyncio.run(svc.process("年假有几天？"))

    assert resp.partial is True
    assert resp.answer == "请求处理超时，请稍后重试。"
    assert resp.refused is False
    assert resp.token_usage == {}
    deps["cache"].put.assert_not_awaited()          # 降级话术不写缓存
    deps["postprocessor"].process.assert_not_called()  # 系统固定文案跳过拒答/脱敏

    metrics = asyncio.run(_fetch_metrics())
    assert metrics[0]["timeout"] == 1
    turns = asyncio.run(_fetch_turns(resp.session_id))
    assert len(turns) == 1


def test_chat_service_refused(tmp_path):
    """拒答路径：postprocessor 返回 refused=True → ChatResponse.refused=True"""
    refusal = PostProcessResult(
        answer="抱歉，我无法在内部知识库中找到与您问题足够相关的信息。",
        refused=True, refusal_reason="low_confidence")
    svc, deps = _chat_svc(tmp_path, pp_result=refusal)

    resp = asyncio.run(svc.process("年假有几天？"))

    assert resp.refused is True
    assert resp.refusal_reason == "low_confidence"
    assert "抱歉" in resp.answer

    metrics = asyncio.run(_fetch_metrics())
    assert metrics[0]["refused"] == 1
    assert metrics[0]["refusal_reason"] == "low_confidence"


def test_chat_service_multi_turn(tmp_path):
    """多轮：第二次 process 带 session_id → turns 2 条 + turn_index 递增 + 会话复用"""
    svc, deps = _chat_svc(tmp_path)

    r1 = asyncio.run(svc.process("第一问"))
    r2 = asyncio.run(svc.process("第二问", session_id=r1.session_id))

    assert r2.session_id == r1.session_id
    turns = asyncio.run(_fetch_turns(r1.session_id))
    assert len(turns) == 2
    assert [t["turn_index"] for t in turns] == [0, 1]
    assert [t["raw_query"] for t in turns] == ["第一问", "第二问"]

    sessions = asyncio.run(_fetch_sessions())
    assert len(sessions) == 1                        # 会话不重复创建
    assert sessions[0]["turn_count"] == 2
    metrics = asyncio.run(_fetch_metrics())
    assert len(metrics) == 2


def test_chat_service_cache_disabled(tmp_path):
    """cache.l1.enabled=false → 跳过缓存查询与写入"""
    svc, deps = _chat_svc(tmp_path, cache_enabled=False)

    resp = asyncio.run(svc.process("年假有几天？"))

    assert resp.from_cache is False
    deps["cache"].get.assert_not_awaited()
    deps["cache"].put.assert_not_awaited()


def test_chat_service_mode_override(tmp_path):
    """请求级 mode 覆盖：检索/缓存 key 用生效 mode；config 保持原值不被改写"""
    svc, deps = _chat_svc(tmp_path)

    resp = asyncio.run(svc.process("年假有几天？", None, "vector-only"))

    assert resp.mode == "vector-only"
    deps["retrieval"].retrieve.assert_awaited_with("年假有几天？", "general", "vector-only")
    deps["cache"].get.assert_awaited_with("年假有几天？", "vector-only")  # 缓存 key 用生效 mode
    deps["cache"].put.assert_awaited_once()
    assert ConfigRegistry.get("retrieval.mode", "") == "hybrid+rerank"   # 全局配置未被污染


# ═══ 3. IngestService ═══════════════════════════════════════

def _ingest_chunks():
    return [
        Chunk(chunk_id=f"chunk-{i}", text=f"手册内容 {i}",
              heading_path="员工手册 > 第三章", language="zh",
              metadata={"source_file": "handbook.pdf",
                        "heading_path": "员工手册 > 第三章", "chunk_index": i})
        for i in range(2)
    ]


def _ingest_db(tmp_path):
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("paths.sqlite", str(tmp_path / "ingest.db"))
    asyncio.run(init_db())


def test_ingest_service_pipeline_order(tmp_path):
    """验证调用顺序：hash → check → OCR → chunk → tag → dedup → embed → commit → conflict → build_index"""
    _ingest_db(tmp_path)
    store = MagicMock()
    store.collection.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    bm25 = MagicMock()
    calls = []
    chunks = _ingest_chunks()

    with patch("services.ingest.OCRPipeline") as ocr_cls, \
         patch("services.ingest.HierarchicalChunker") as chunker_cls, \
         patch("core.embedder.Embedder") as embedder_cls, \
         patch("services.ingest.VersionedIngestService") as versioned_cls, \
         patch("services.ingest.BilingualHandler") as bilingual_cls, \
         patch("services.ingest.DedupPipeline") as dedup_cls, \
         patch("services.ingest.ConflictDetector") as conflict_cls:
        ocr_cls.return_value.process.side_effect = (
            lambda fp: calls.append("ocr") or ParsedDoc(
                text="全文", pages=[], tables=[], source=fp, language="zh"))
        chunker_cls.return_value.chunk.side_effect = (
            lambda doc: calls.append("chunk") or list(chunks))
        bilingual_cls.return_value.tag_chunks.side_effect = (
            lambda cs: calls.append("tag") or list(cs))
        dedup_cls.return_value.exact_dedup.side_effect = (
            lambda cs: calls.append("dedup") or list(cs))
        embedder_cls.return_value.encode.side_effect = (
            lambda texts: calls.append("embed") or [[0.1] * 8 for _ in texts])
        versioned_cls.return_value.compute_hash.side_effect = (
            lambda fp: calls.append("hash") or "deadbeef")
        versioned_cls.return_value.check_exists.side_effect = (
            lambda h: calls.append("check") or False)
        versioned_cls.return_value.commit.side_effect = (
            lambda *a, **k: calls.append("commit") or IngestResult(
                status="ingested", chunks_created=len(chunks), chunks_replaced=0,
                doc_hash="deadbeef", source_file="handbook.pdf", version="v1.0"))
        conflict_cls.return_value.detect.side_effect = (
            lambda new, old: calls.append("conflict") or [])
        bm25.build_index.side_effect = lambda: calls.append("build_index")

        svc = IngestService(chroma_store=store, bm25_retriever=bm25)
        result = svc.ingest("assets/corpus/handbook.pdf", "handbook", "v1.0")

    assert result.status == "ingested"
    assert result.chunks_created == 2
    assert calls == ["hash", "check", "ocr", "chunk", "tag", "dedup",
                     "embed", "commit", "conflict", "build_index"]
    assert chunks[0].embedding == [0.1] * 8          # 向量已写回 chunk
    bm25.build_index.assert_called_once()

    logs = asyncio.run(_fetch_ingest_logs())
    assert len(logs) == 1
    assert logs[0]["status"] == "ingested"
    assert logs[0]["doc_hash"] == "deadbeef"


def test_ingest_service_commit_skipped(tmp_path):
    """mock commit 返回 skipped → ingest 结果 status='skipped'，仍重建 BM25 索引并记日志"""
    _ingest_db(tmp_path)
    store = MagicMock()
    bm25 = MagicMock()
    with patch("services.ingest.OCRPipeline") as ocr_cls, \
         patch("services.ingest.HierarchicalChunker") as chunker_cls, \
         patch("core.embedder.Embedder") as embedder_cls, \
         patch("services.ingest.VersionedIngestService") as versioned_cls:
        ocr_cls.return_value.process.return_value = ParsedDoc(
            text="全文", pages=[], tables=[], source="f", language="zh")
        chunker_cls.return_value.chunk.return_value = _ingest_chunks()
        embedder_cls.return_value.encode.return_value = [[0.1] * 8, [0.1] * 8]
        versioned_cls.return_value.compute_hash.return_value = "abc123"
        versioned_cls.return_value.check_exists.return_value = False
        versioned_cls.return_value.commit.return_value = IngestResult(
            status="skipped", doc_hash="abc123", source_file="handbook.pdf",
            version="v1.0", reason="文档内容未变（相同 doc_hash 已入库且 is_active）")

        svc = IngestService(chroma_store=store, bm25_retriever=bm25)
        result = svc.ingest("assets/corpus/handbook.pdf", "handbook")

    assert result.status == "skipped"
    bm25.build_index.assert_called_once()

    logs = asyncio.run(_fetch_ingest_logs())
    assert logs[0]["status"] == "skipped"


def test_ingest_service_check_exists_early_skip(tmp_path):
    """check_exists=True → 早退返回 skipped，OCR 与索引重建均不执行"""
    _ingest_db(tmp_path)
    store = MagicMock()
    bm25 = MagicMock()
    with patch("services.ingest.OCRPipeline") as ocr_cls, \
         patch("services.ingest.VersionedIngestService") as versioned_cls:
        versioned_cls.return_value.compute_hash.return_value = "abc123"
        versioned_cls.return_value.check_exists.return_value = True

        svc = IngestService(chroma_store=store, bm25_retriever=bm25)
        result = svc.ingest("assets/corpus/handbook.pdf", "handbook")

    assert result.status == "skipped"
    assert result.reason
    ocr_cls.return_value.process.assert_not_called()
    bm25.build_index.assert_not_called()
