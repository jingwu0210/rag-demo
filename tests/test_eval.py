"""Task 9: Evaluation Pipeline 测试 — 测试集加载 / 三配置 Runner / 报表与前后对比

- mock chat_service（FakeChatService 返回固定 ChatResponse，out_of_scope 问题拒答）
- Ragas LLM judge 用 patch("eval.runner._try_ragas_score") 隔离，不依赖 API key / 网络
- eval_history 用 tempfile SQLite 真实验证（override paths.sqlite）
"""
import asyncio
import csv
import json
import os
from unittest.mock import MagicMock, patch

from core.config import ConfigRegistry
from eval.report import compare_runs, generate_report
from eval.runner import apply_config_mode, compute_test_set_hash, run_comparison
from eval.test_set import SAMPLE_TEST_SET, load_test_set
from services.chat import ChatResponse
from storage.sqlite_client import get_db, init_db


# ═══ helpers ═══════════════════════════════════════════════

class FakeChatService:
    """固定 ChatResponse；含"天气"的问题 → out_of_scope 拒答（refused=True）。"""

    def __init__(self):
        self.calls = []

    async def process(self, query: str, session_id: str = None) -> ChatResponse:
        self.calls.append(query)
        if "天气" in query:
            return ChatResponse(answer="", session_id="s", refused=True,
                                refusal_reason="out_of_scope")
        return ChatResponse(
            answer=f"mock answer for {query}",
            session_id="s",
            sources=[{"chunk_id": "c1", "heading_path": "员工手册 > 第三章", "score": 0.9}],
            timing_ms={"retrieval": 10, "rerank": 5, "generation": 60, "total": 100},
            token_usage={"prompt": 100, "completion": 100, "total": 200},
        )


async def _fetch_eval_rows(run_id: str):
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM eval_history WHERE run_id = ? ORDER BY config_name", (run_id,))
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _insert_eval_rows(run_id: str, faithfulness: float,
                            p50: int, p95: int, test_set_hash: str = "same_hash"):
    """手工插入一批 eval_history（3 config，指标值随入参）"""
    db = await get_db()
    try:
        for cfg in ["vector-only", "hybrid", "hybrid+rerank"]:
            await db.execute(
                "INSERT INTO eval_history (run_id, config_name, test_set_hash, total_qa_pairs, "
                "faithfulness, context_precision, answer_compliance, "
                "refusal_appropriateness, style_consistency, p50_latency_ms, "
                "p95_latency_ms, avg_tokens_per_call) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, cfg, test_set_hash, 5, faithfulness, faithfulness + 0.1,
                 0.8, 1.0, None, p50, p95, 300))
        await db.commit()
    finally:
        await db.close()


def _sample_test_set():
    return [dict(t) for t in SAMPLE_TEST_SET]


def _init_sqlite(tmp_path: str) -> None:
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("paths.sqlite", os.path.join(str(tmp_path), "eval.db"))
    asyncio.run(init_db())


# ═══ 1. 测试集加载 ═════════════════════════════════════════

def test_load_test_set_fallback_when_file_missing(tmp_path):
    ConfigRegistry.init("config.yaml")
    path = os.path.join(str(tmp_path), "not_exist.json")
    test_set = load_test_set(path)
    assert len(test_set) == 5
    assert sum(1 for t in test_set if t["is_out_of_scope"]) == 1
    assert test_set[0]["question"] == "员工年假天数是多少？"
    # 返回副本，不共享内部对象
    test_set[0]["question"] = "改"
    assert SAMPLE_TEST_SET[0]["question"] == "员工年假天数是多少？"


def test_load_test_set_from_file(tmp_path):
    ConfigRegistry.init("config.yaml")
    path = os.path.join(str(tmp_path), "test_set.json")
    data = [{"question": "Q1", "ground_truth": "A1", "relevant_chunks": ["c1"],
             "language": "zh", "is_out_of_scope": False}]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    test_set = load_test_set(path)
    assert len(test_set) == 1
    assert test_set[0]["question"] == "Q1"
    assert test_set[0]["relevant_chunks"] == ["c1"]


# ═══ 2. 配置模式 ═══════════════════════════════════════════

def test_apply_config_mode():
    ConfigRegistry.init("config.yaml")
    apply_config_mode("vector-only")
    assert ConfigRegistry.get("retrieval.mode") == "vector-only"
    assert ConfigRegistry.get("reranker.enabled") is False

    apply_config_mode("hybrid")
    assert ConfigRegistry.get("retrieval.mode") == "hybrid"
    assert ConfigRegistry.get("reranker.enabled") is False

    apply_config_mode("hybrid+rerank")
    assert ConfigRegistry.get("retrieval.mode") == "hybrid+rerank"
    assert ConfigRegistry.get("reranker.enabled") is True


# ═══ 3. 三配置对比 Runner ══════════════════════════════════

def test_run_comparison_executes_three_configs(tmp_path):
    _init_sqlite(tmp_path)
    service = FakeChatService()
    test_set = _sample_test_set()
    with patch("eval.runner._try_ragas_score", return_value=0.8):
        results = run_comparison(service, test_set, run_id="run_e2e")

    # 3 个配置都被执行
    assert len(results) == 3
    assert {r["config_name"] for r in results} == {"vector-only", "hybrid", "hybrid+rerank"}
    # 每条 QA 都调用了 chat_service
    assert len(service.calls) == 3 * 5

    for r in results:
        assert r["run_id"] == "run_e2e"
        assert r["test_set_hash"] == compute_test_set_hash(test_set)
        assert r["total_requests"] == 5
        # Ragas 指标（patch 固定 0.8）
        assert r["faithfulness"] == 0.8
        assert r["context_precision"] == 0.8
        # 规则近似：5 条中 4 条非 OOS 且带 sources 答 → 4/5
        assert r["answer_compliance"] == 0.8
        # 拒答恰当性：4 条非 OOS 未拒 + 1 条 OOS 拒答 → 5/5
        assert r["refusal_appropriateness"] == 1.0
        assert r["style_consistency"] is None
        assert r["p50_latency_ms"] == 100
        assert r["p95_latency_ms"] == 100
        # OOS 拒答无 token → (200*4 + 0) / 5
        assert r["avg_tokens_per_call"] == 160

    # eval_history 表有 3 条记录
    rows = asyncio.run(_fetch_eval_rows("run_e2e"))
    assert len(rows) == 3
    assert {r["config_name"] for r in rows} == {"vector-only", "hybrid", "hybrid+rerank"}
    assert all(r["refusal_appropriateness"] == 1.0 for r in rows)
    assert all(r["test_set_hash"] == compute_test_set_hash(test_set) for r in rows)
    # per_qa_results_json 含逐条明细
    per_qa = json.loads(rows[0]["per_qa_results_json"])
    assert len(per_qa) == 5
    oos = [p for p in per_qa if p["is_out_of_scope"]]
    assert len(oos) == 1 and oos[0]["refused"] is True and oos[0]["answer_compliance"] == 0


def test_apply_config_mode_switches_retriever_dispatch():
    """C-1 回归：已构造的 Retriever 门面在 apply_config_mode 切换后分发到不同子检索器。

    用真实 Retriever 门面 + mock 子检索器：override mode 不重建门面也能切换实现，
    且切回同一 mode 复用缓存的子检索器（不重复构造）。
    """
    from core.retriever import Retriever

    ConfigRegistry.init("config.yaml")
    with patch("core.retriever.VectorRetriever") as mock_vec, \
            patch("core.retriever.HybridRetriever") as mock_hybrid:
        retriever = Retriever(chroma_store=MagicMock(), embedder=MagicMock())

        # 初始 hybrid → 分发到 HybridRetriever
        apply_config_mode("hybrid")
        retriever.retrieve("q1", top_k=2)
        assert mock_hybrid.call_count == 1
        assert mock_hybrid.return_value.retrieve.call_count == 1
        assert mock_vec.call_count == 0

        # 切到 vector-only → 同一门面实例分发到 VectorRetriever
        apply_config_mode("vector-only")
        retriever.retrieve("q2", top_k=2)
        assert mock_vec.call_count == 1
        assert mock_vec.return_value.retrieve.call_count == 1
        assert mock_hybrid.return_value.retrieve.call_count == 1  # hybrid 不再被调用

        # 切回 hybrid → 复用缓存的 HybridRetriever（不重复构造）
        apply_config_mode("hybrid")
        retriever.retrieve("q3", top_k=2)
        assert mock_hybrid.call_count == 1
        assert mock_hybrid.return_value.retrieve.call_count == 2

        # hybrid+rerank 粗排侧与 hybrid 共用 HybridRetriever
        apply_config_mode("hybrid+rerank")
        retriever.retrieve("q4", top_k=2)
        assert mock_hybrid.call_count == 1
        assert mock_hybrid.return_value.retrieve.call_count == 3


def test_run_comparison_ragas_import_failure_does_not_abort(tmp_path, monkeypatch):
    """I-1 回归：Ragas 不可导入（SingleTurnSample=None）→ 评估不中断，规则指标照常。"""
    import eval.runner as eval_runner

    _init_sqlite(tmp_path)
    monkeypatch.setattr(eval_runner, "SingleTurnSample", None)
    monkeypatch.setattr(eval_runner, "_RAGAS_IMPORTED", False)
    monkeypatch.setattr(eval_runner, "_RAGAS_LLM_READY", False)

    service = FakeChatService()
    results = run_comparison(service, _sample_test_set(), run_id="run_no_ragas_import")

    assert len(results) == 3
    for r in results:
        assert r["faithfulness"] is None
        assert r["context_precision"] is None
        # 规则近似指标照常
        assert r["answer_compliance"] == 0.8
        assert r["refusal_appropriateness"] == 1.0
    rows = asyncio.run(_fetch_eval_rows("run_no_ragas_import"))
    assert len(rows) == 3


def test_run_comparison_ragas_unavailable_yields_none(tmp_path):
    """无 LLM judge（_try_ragas_score 返回 None）→ Ragas 指标为 None，评估不中断"""
    _init_sqlite(tmp_path)
    service = FakeChatService()
    with patch("eval.runner._try_ragas_score", return_value=None):
        results = run_comparison(service, _sample_test_set(), run_id="run_no_ragas")
    assert len(results) == 3
    assert all(r["faithfulness"] is None for r in results)
    assert all(r["context_precision"] is None for r in results)
    # 规则近似指标仍然可用
    assert all(r["answer_compliance"] == 0.8 for r in results)
    assert all(r["refusal_appropriateness"] == 1.0 for r in results)


# ═══ 3.5 retrieved_contexts 保真（I-4 终审）══════════════

def test_build_ragas_sample_prefers_source_text():
    """I-4：retrieved_contexts 用 chunk 真实文本（chat sources 的 text 字段）而非 heading_path"""
    from eval.runner import _build_ragas_sample

    sample = _build_ragas_sample(
        "Q", "A",
        sources=[{"chunk_id": "c1", "heading_path": "员工手册 > 第三章",
                  "text": "年假为 10 天", "score": 0.9}],
        ground_truth="GT")
    if sample is None:
        pytest.skip("ragas 不可导入")
    assert sample.retrieved_contexts == ["年假为 10 天"]


def test_build_ragas_sample_falls_back_to_heading_path():
    """I-4：无 text（旧结构/mock）→ 回退 heading_path，不中断评估"""
    from eval.runner import _build_ragas_sample

    sample = _build_ragas_sample(
        "Q", "A",
        sources=[{"chunk_id": "c1", "heading_path": "员工手册 > 第三章", "score": 0.9}],
        ground_truth="GT")
    if sample is None:
        pytest.skip("ragas 不可导入")
    assert sample.retrieved_contexts == ["员工手册 > 第三章"]


# ═══ 4. 报表生成 ═══════════════════════════════════════════

def test_generate_report_writes_csv_and_markdown(tmp_path):
    ConfigRegistry.init("config.yaml")
    results = [
        {"config_name": "vector-only", "faithfulness": 0.5, "context_precision": 0.4,
         "answer_compliance": 0.8, "refusal_appropriateness": 1.0, "style_consistency": None,
         "p50_latency_ms": 100, "p95_latency_ms": 200, "avg_tokens_per_call": 300,
         "total_requests": 5},
        {"config_name": "hybrid", "faithfulness": 0.6, "context_precision": 0.5,
         "answer_compliance": 0.8, "refusal_appropriateness": 1.0, "style_consistency": None,
         "p50_latency_ms": 90, "p95_latency_ms": 180, "avg_tokens_per_call": 290,
         "total_requests": 5},
        {"config_name": "hybrid+rerank", "faithfulness": 0.7, "context_precision": 0.6,
         "answer_compliance": 0.8, "refusal_appropriateness": 1.0, "style_consistency": None,
         "p50_latency_ms": 80, "p95_latency_ms": 170, "avg_tokens_per_call": 280,
         "total_requests": 5},
    ]
    output_dir = os.path.join(str(tmp_path), "reports")
    csv_path = generate_report(results, output_dir)

    # CSV 存在且列齐全 + 三配置行
    assert os.path.exists(csv_path)
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["config", "faithfulness", "context_precision", "answer_compliance",
                       "refusal_appropriateness", "style_consistency", "p50_ms", "p95_ms",
                       "avg_tokens", "total_requests"]
    assert len(rows) == 4  # header + 3 config
    configs = {row[0] for row in rows[1:]}
    assert configs == {"vector-only", "hybrid", "hybrid+rerank"}
    # 值正确（None → 空串）
    hybrid_row = [r for r in rows[1:] if r[0] == "hybrid"][0]
    assert hybrid_row[1] == "0.6" and hybrid_row[5] == "" and hybrid_row[8] == "290"

    # Markdown 报告存在，结论段标注最优配置（hybrid+rerank 的 faithfulness 最优）
    md_path = os.path.join(output_dir, "eval_report.md")
    assert os.path.exists(md_path)
    with open(md_path, encoding="utf-8") as f:
        md = f.read()
    assert "Faithfulness" in md
    assert "hybrid+rerank" in md and "0.7" in md


# ═══ 5. before/after 对比 ══════════════════════════════════

def test_compare_runs_delta_pct(tmp_path):
    _init_sqlite(tmp_path)
    asyncio.run(_insert_eval_rows("run_before", faithfulness=0.5, p50=100, p95=200))
    asyncio.run(_insert_eval_rows("run_after", faithfulness=0.8, p50=80, p95=150))

    result = compare_runs("run_before", "run_after")

    assert set(result.keys()) == {"vector-only", "hybrid", "hybrid+rerank"}
    hybrid = result["hybrid"]
    assert hybrid["faithfulness"] == {"before": 0.5, "after": 0.8, "delta_pct": 60.0}
    assert hybrid["p50_latency_ms"] == {"before": 100, "after": 80, "delta_pct": -20.0}
    assert hybrid["p95_latency_ms"] == {"before": 200, "after": 150, "delta_pct": -25.0}
    assert hybrid["context_precision"] == {"before": 0.6, "after": 0.9, "delta_pct": 50.0}
    # 无变化 → 0.0；before 缺失 → None
    assert result["vector-only"]["refusal_appropriateness"] == {"before": 1.0, "after": 1.0,
                                                                "delta_pct": 0.0}
    assert result["vector-only"]["style_consistency"]["delta_pct"] is None


def test_compare_runs_test_set_mismatch_raises(tmp_path):
    _init_sqlite(tmp_path)
    asyncio.run(_insert_eval_rows("run_before", faithfulness=0.5, p50=100, p95=200))
    asyncio.run(_insert_eval_rows("run_after", faithfulness=0.8, p50=80, p95=150,
                                  test_set_hash="other_hash"))
    try:
        compare_runs("run_before", "run_after")
        assert False, "should raise ValueError"
    except ValueError as exc:
        assert "test set mismatch" in str(exc)
