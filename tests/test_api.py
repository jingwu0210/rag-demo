"""Task 8: API Layer 测试 — FastAPI TestClient

- 不使用 `with TestClient(app)`：避免触发 startup（会真实加载 BGE-M3 等重组件），
  直接替换 app.state 的 mock 服务（依赖注入验收）
- Python 3.9 的 asyncio.Semaphore 绑定事件循环：guard mock 的 acquire 在请求
  所在 loop 内创建信号量，避免跨 loop 报错
- 异步测试用 asyncio.run 包裹（项目未装 pytest-asyncio）
"""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import app
from core.config import ConfigRegistry
from core.guard import ConcurrencyLimitExceeded, ResilienceGuard
from core.versioned import IngestResult
from services.chat import ChatResponse
from storage.sqlite_client import get_db, init_db


@pytest.fixture(autouse=True)
def _config(tmp_path):
    """每个测试独立配置：ConfigRegistry 初始化 + sqlite 指向临时目录"""
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("paths.sqlite", str(tmp_path / "test.db"))
    yield


def _mock_guard():
    """guard mock：acquire 在请求 loop 内创建真实信号量；
    with_request_timeout 用真实超时语义（wait_for 9s）"""
    guard = MagicMock()

    async def acquire():
        return asyncio.Semaphore(10)

    async def with_request_timeout(coro):
        return await asyncio.wait_for(coro, timeout=9)

    guard.acquire = acquire
    guard.with_request_timeout = with_request_timeout
    return guard


def _full_response(session_id="sess-123"):
    return ChatResponse(
        answer="根据《员工手册》规定，年假为 10 天。",
        session_id=session_id,
        sources=[{"chunk_id": "c1", "heading_path": "员工手册 > 第三章", "score": 0.91,
                  "text": "根据《员工手册》第三章，年假为 10 天。"}],
        timing_ms={"retrieval": 12, "rerank": 8, "generation": 50, "total": 70},
        token_usage={"prompt": 10, "completion": 20, "total": 30},
        refused=False, refusal_reason=None, from_cache=False, partial=False,
        mode="hybrid+rerank",
    )


# ═══ 1. /chat ══════════════════════════════════════════════

def test_chat_success():
    """成功路径：完整 ChatResponse → 200 + 字段齐全；无 session_id → process 传 None"""
    svc = MagicMock()
    svc.process = AsyncMock(return_value=_full_response())
    app.state.chat_service = svc
    app.state.guard = _mock_guard()

    client = TestClient(app)
    r = client.post("/chat", json={"query": "年假有几天？"})

    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "根据《员工手册》规定，年假为 10 天。"
    assert body["session_id"] == "sess-123"
    assert body["sources"] == [{"chunk_id": "c1", "heading_path": "员工手册 > 第三章",
                                "score": 0.91, "text": "根据《员工手册》第三章，年假为 10 天。"}]
    assert body["timing_ms"] == {"retrieval": 12, "rerank": 8, "generation": 50, "total": 70}
    assert body["token_usage"] == {"prompt": 10, "completion": 20, "total": 30}
    assert body["refused"] is False
    assert body["refusal_reason"] is None
    assert body["from_cache"] is False
    assert body["partial"] is False
    assert body["mode"] == "hybrid+rerank"    # 响应携带实际生效模式
    # 向后兼容：不传 mode → process 收到 None（由服务层回落到 config）
    svc.process.assert_awaited_with("年假有几天？", None, None)


def test_chat_mode_override():
    """请求级 mode：透传 process + 响应 mode 反映覆盖值"""
    resp = _full_response()
    resp.mode = "vector-only"
    svc = MagicMock()
    svc.process = AsyncMock(return_value=resp)
    app.state.chat_service = svc
    app.state.guard = _mock_guard()

    r = TestClient(app).post("/chat", json={"query": "年假有几天？", "mode": "vector-only"})

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "vector-only"
    svc.process.assert_awaited_with("年假有几天？", None, "vector-only")


def test_chat_unknown_mode_422():
    """非法 mode 值 → 422（不落入 process，避免下游 ValueError → 500）"""
    svc = MagicMock()
    svc.process = AsyncMock()
    app.state.chat_service = svc
    app.state.guard = _mock_guard()

    r = TestClient(app).post("/chat", json={"query": "q", "mode": "bm25"})

    assert r.status_code == 422
    assert "未知检索模式" in r.json()["detail"]
    svc.process.assert_not_awaited()


def test_chat_mode_timeout_dict_path():
    """超时 dict 路径：响应 mode 回落为请求生效模式（不 500）"""
    svc = MagicMock()
    svc.process = AsyncMock(return_value={"partial": True, "timeout": True})
    app.state.chat_service = svc
    app.state.guard = _mock_guard()

    r = TestClient(app).post("/chat", json={"query": "q", "mode": "hybrid"})

    assert r.status_code == 200
    assert r.json()["partial"] is True
    assert r.json()["mode"] == "hybrid"


def test_chat_cache_hit_session_none():
    """集成事实：缓存命中时 session_id 可能为 None → 归一为空串，不 500"""
    resp = _full_response(session_id=None)
    resp.from_cache = True
    svc = MagicMock()
    svc.process = AsyncMock(return_value=resp)
    app.state.chat_service = svc
    app.state.guard = _mock_guard()

    with patch("api.routes._session_exists", new=AsyncMock(return_value=True)):
        r = TestClient(app).post("/chat", json={"query": "年假有几天？", "session_id": "sess-1"})

    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == ""
    assert body["from_cache"] is True


def test_chat_concurrency_limit_429():
    """并发超限：guard.acquire 抛 ConcurrencyLimitExceeded → 429 + Retry-After"""
    guard = MagicMock()
    guard.acquire = AsyncMock(side_effect=ConcurrencyLimitExceeded())
    app.state.chat_service = MagicMock()
    app.state.guard = guard

    r = TestClient(app).post("/chat", json={"query": "年假有几天？"})

    assert r.status_code == 429
    assert r.headers.get("retry-after") == "1"
    assert "繁忙" in r.json()["detail"]


def test_chat_request_timeout_partial():
    """请求硬超时：with_request_timeout 返回 {"partial": True} dict → 200 + partial=true"""
    svc = MagicMock()
    svc.process = AsyncMock(return_value={"partial": True, "timeout": True})
    app.state.chat_service = svc
    app.state.guard = _mock_guard()

    r = TestClient(app).post("/chat", json={"query": "年假有几天？"})

    assert r.status_code == 200
    body = r.json()
    assert body["partial"] is True
    assert body["answer"] == "请求处理超时，请稍后重试。"


def test_chat_valid_session_passthrough():
    """M-3：session_id 存在于 sessions 表 → 原样传给 process"""
    svc = MagicMock()
    svc.process = AsyncMock(return_value=_full_response("sess-ok"))
    app.state.chat_service = svc
    app.state.guard = _mock_guard()

    with patch("api.routes._session_exists", new=AsyncMock(return_value=True)):
        r = TestClient(app).post("/chat", json={"query": "q", "session_id": "sess-ok"})

    assert r.status_code == 200
    assert r.json()["session_id"] == "sess-ok"
    svc.process.assert_awaited_with("q", "sess-ok", None)


def test_chat_invalid_session_recreated():
    """M-3：无效 session_id（turns 外键约束）→ 创建新会话替代，避免 IntegrityError"""
    svc = MagicMock()
    svc.process = AsyncMock(return_value=_full_response("new-session-abc"))
    svc._create_session = AsyncMock(return_value="new-session-abc")
    app.state.chat_service = svc
    app.state.guard = _mock_guard()

    with patch("api.routes._session_exists", new=AsyncMock(return_value=False)):
        r = TestClient(app).post("/chat", json={"query": "q", "session_id": "stale-session"})

    assert r.status_code == 200
    assert r.json()["session_id"] == "new-session-abc"
    svc._create_session.assert_awaited_once()
    svc.process.assert_awaited_with("q", "new-session-abc", None)


# ═══ 2. /ingest ════════════════════════════════════════════

def test_ingest_success(tmp_path):
    """上传小文本文件 → 保存到 corpus → ingest 返回 replaced → 200 + 字段正确"""
    ConfigRegistry.override("paths.corpus", str(tmp_path / "corpus"))
    svc = MagicMock()
    svc.ingest = MagicMock(return_value=IngestResult(
        status="replaced", chunks_created=2, chunks_replaced=5,
        doc_hash="abc123def", source_file="handbook.txt", version="v1.1"))
    app.state.ingest_service = svc

    r = TestClient(app).post(
        "/ingest",
        files={"file": ("handbook.txt", "2025 年假规定：10 天。".encode("utf-8"), "text/plain")},
        data={"doc_type": "handbook", "version": "v1.1"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "replaced"
    assert body["chunks_created"] == 2
    assert body["chunks_replaced"] == 5
    assert body["doc_hash"] == "abc123def"
    assert body["source_file"] == "handbook.txt"
    assert body["version"] == "v1.1"

    # 文件已保存到 corpus 目录，ingest 以落盘路径 + doc_type + version 调用
    saved = tmp_path / "corpus" / "handbook.txt"
    assert saved.read_bytes() == "2025 年假规定：10 天。".encode("utf-8")
    svc.ingest.assert_called_once()
    args = svc.ingest.call_args[0]
    assert args[0] == str(saved)
    assert args[1:] == ("handbook", "v1.1")


def test_ingest_service_error_500():
    """ingest 抛异常 → 500 + 明确错误信息"""
    ConfigRegistry.override("paths.corpus", "/tmp/__no_such_corpus__")
    svc = MagicMock()
    svc.ingest = MagicMock(side_effect=RuntimeError("OCR 失败"))
    app.state.ingest_service = svc

    r = TestClient(app).post(
        "/ingest",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"doc_type": "handbook"},
    )

    assert r.status_code == 500
    assert "OCR 失败" in r.json()["detail"]


# ═══ 3. /health ════════════════════════════════════════════

def test_health_ok():
    """全组件正常 → 200 + status=ok + components/concurrency 结构正确"""
    store = MagicMock()
    store.collection.count.return_value = 5
    app.state.chroma_store = store
    app.state.guard = ResilienceGuard()   # 真实 guard：semaphore 在请求 loop 内惰性创建

    r = TestClient(app).get("/health")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["components"] == {"chromadb": "ok", "sqlite": "ok", "llm": "ok"}
    assert body["concurrency"] == {"active": 0, "max": 10}


def test_health_degraded_on_component_failure():
    """组件探测失败 / startup 失败 → status=degraded + 对应组件 error"""
    store = MagicMock()
    store.collection.count.side_effect = RuntimeError("chroma 不可用")
    app.state.chroma_store = store
    app.state.guard = None
    app.state.startup_error = "Embedder 模型下载失败"

    r = TestClient(app).get("/health")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["components"]["chromadb"] == "error"
    assert body["components"]["llm"] == "error"
    assert body["components"]["sqlite"] == "ok"
    assert body["concurrency"] == {"active": 0, "max": 10}


def test_startup_warmup_failure_does_not_crash(tmp_path):
    """B 方案防御承诺：startup 预热 reranker 失败 → warning，不置 startup_error。

    服务照常启动（运行时每个请求仍有 ensure_loaded + rerank 降级兜底）。
    patch 重组件构造：避免真实加载 BGE-M3/BGE-Reranker（test 约定）。
    """
    from api.app import startup

    ConfigRegistry.override("chromadb.persist_directory", str(tmp_path / "chroma"))
    mock_reranker = MagicMock()
    mock_reranker.ensure_loaded.side_effect = RuntimeError("预热失败")
    mock_reranker_cls = MagicMock(return_value=mock_reranker)

    with patch("core.embedder.Embedder"), \
         patch("core.reranker.Reranker", mock_reranker_cls), \
         patch("core.retriever.Retriever"), \
         patch("storage.chroma_client.ChromaStore"):
        asyncio.run(startup())

    assert app.state.startup_error is None
    mock_reranker.ensure_loaded.assert_called_once()
    # 组件单例仍已挂载（预热失败不影响启动）
    assert app.state.chat_service is not None


# ═══ 4. 评估触发 / 结果查询 / 运营报表（I-2 终审接线）═══════

async def _insert_metrics_rows():
    """插入 3 行 request_metrics：2 次缓存命中 + 1 次拒答，latency 100/200/300"""
    db = await get_db()
    try:
        for i, (lat, cache_hit, refused) in enumerate(
                [(100, 1, 0), (200, 1, 0), (300, 0, 1)]):
            await db.execute(
                "INSERT INTO request_metrics (request_id, session_id, latency_total, "
                "token_total, retrieval_mode, cache_hit, refused, pii_redact_count, "
                "injection_blocked) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"req-{i}", "s1", lat, 30, "hybrid+rerank",
                 cache_hit, refused, 1, 2))
        await db.commit()
    finally:
        await db.close()


async def _insert_eval_rows(run_id: str, faithfulness: float = 0.7):
    """插入 3 行 eval_history（三配置各一条）"""
    db = await get_db()
    try:
        for cfg in ["vector-only", "hybrid", "hybrid+rerank"]:
            await db.execute(
                "INSERT INTO eval_history (run_id, config_name, test_set_hash, "
                "total_qa_pairs, faithfulness, context_precision, answer_compliance, "
                "refusal_appropriateness, p50_latency_ms, p95_latency_ms, "
                "avg_tokens_per_call) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, cfg, "hash1", 5, faithfulness, 0.8, 0.9, 1.0, 100, 150, 300))
        await db.commit()
    finally:
        await db.close()


def test_report_csv_with_metrics():
    """GET /report：request_metrics 聚合 → 200 + text/csv + 表头与数据行"""
    asyncio.run(init_db())
    asyncio.run(_insert_metrics_rows())

    r = TestClient(app).get("/report")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    body = r.text
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    assert lines[0] == "metric,value"
    metrics = dict(ln.split(",") for ln in lines[1:])
    assert metrics["total_requests"] == "3"
    assert metrics["p50_latency_ms"] == "200"       # numpy.percentile(50)
    assert metrics["p95_latency_ms"] == "290"       # numpy.percentile(95)
    assert metrics["total_tokens"] == "90"
    assert metrics["avg_tokens_per_call"] == "30.0"
    assert metrics["cache_hit_rate"] == "0.6667"    # 2/3
    assert metrics["refusal_rate"] == "0.3333"      # 1/3
    assert metrics["pii_redactions_total"] == "3"
    assert metrics["injections_blocked_total"] == "6"


def test_report_csv_empty_table():
    """GET /report 无数据 → 200 + 仅表头 CSV"""
    asyncio.run(init_db())

    r = TestClient(app).get("/report")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.strip() == "metric,value"


def test_eval_run_returns_202_with_run_id():
    """POST /eval/run → 202 + {run_id}；后台任务经 to_thread 启动（mock 为 no-op）"""
    svc = MagicMock()
    app.state.chat_service = svc

    with patch("api.routes._run_eval_job") as job:
        r = TestClient(app).post("/eval/run")

    assert r.status_code == 202
    body = r.json()
    assert body["run_id"].startswith("eval_")
    assert len(body["run_id"]) > len("eval_")
    # 后台任务确实被触发（to_thread 包裹，不跑在事件循环线程）
    job.assert_called_once()
    assert job.call_args[0][0] is svc
    assert job.call_args[0][1] == body["run_id"]


def test_eval_result_by_run_id():
    """GET /eval/result?run_id= → 200 + 该 run 各 config 指标 JSON"""
    asyncio.run(init_db())
    asyncio.run(_insert_eval_rows("run_xyz"))

    r = TestClient(app).get("/eval/result", params={"run_id": "run_xyz"})

    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "run_xyz"
    assert len(body["results"]) == 3
    cfgs = {res["config_name"] for res in body["results"]}
    assert cfgs == {"vector-only", "hybrid", "hybrid+rerank"}
    hybrid = [res for res in body["results"] if res["config_name"] == "hybrid"][0]
    assert hybrid["faithfulness"] == 0.7
    assert hybrid["p50_latency_ms"] == 100
    assert hybrid["total_qa_pairs"] == 5


def test_eval_result_latest_run_when_no_run_id():
    """GET /eval/result 无 run_id → 返回最近一次 run"""
    asyncio.run(init_db())
    asyncio.run(_insert_eval_rows("run_old", faithfulness=0.5))
    asyncio.run(_insert_eval_rows("run_new", faithfulness=0.9))

    r = TestClient(app).get("/eval/result")

    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "run_new"     # timestamp 相同 → rowid 更大的为最近
    assert all(res["faithfulness"] == 0.9 for res in body["results"])


def test_eval_result_empty_when_no_history():
    """无 eval_history 记录 → 200 + 空 results"""
    asyncio.run(init_db())

    r = TestClient(app).get("/eval/result")

    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] is None
    assert body["results"] == []


async def _insert_eval_rows_with_per_qa(run_id: str):
    """插入 3 行 eval_history，per_qa_results_json 各配置一条明细"""
    db = await get_db()
    try:
        for i, cfg in enumerate(["vector-only", "hybrid", "hybrid+rerank"]):
            per_qa = json.dumps([
                {"question": f"q{i}-1", "answer": "答", "faithfulness": 0.8,
                 "refused": False, "timeout": False, "sources_count": 2},
                {"question": f"q{i}-2", "answer": "拒", "faithfulness": None,
                 "refused": True, "refusal_reason": "low_confidence",
                 "timeout": False, "sources_count": 0},
            ], ensure_ascii=False)
            await db.execute(
                "INSERT INTO eval_history (run_id, config_name, test_set_hash, "
                "total_qa_pairs, faithfulness, context_precision, answer_compliance, "
                "refusal_appropriateness, p50_latency_ms, p95_latency_ms, "
                "avg_tokens_per_call, per_qa_results_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, cfg, "hash1", 2, 0.7, 0.8, 0.9, 1.0, 100, 150, 300, per_qa))
        await db.commit()
    finally:
        await db.close()


def test_eval_result_detail_per_qa():
    """GET /eval/result?run_id=&detail=true → 追加 per_qa（按 config 分组的明细数组）"""
    asyncio.run(init_db())
    asyncio.run(_insert_eval_rows_with_per_qa("run_det"))

    r = TestClient(app).get("/eval/result", params={"run_id": "run_det", "detail": "true"})

    assert r.status_code == 200
    body = r.json()
    assert set(body["per_qa"].keys()) == {"vector-only", "hybrid", "hybrid+rerank"}
    hybrid = body["per_qa"]["hybrid"]
    assert isinstance(hybrid, list) and len(hybrid) == 2
    assert hybrid[0]["question"] == "q1-1"
    assert hybrid[0]["faithfulness"] == 0.8
    assert hybrid[1]["refused"] is True
    assert hybrid[1]["refusal_reason"] == "low_confidence"
    # 指标行不受影响（不携带 per_qa 明细字段）
    assert "per_qa_results_json" not in body["results"][0]


def test_eval_result_detail_default_false():
    """detail 缺省 false → 响应无 per_qa 键（向后兼容）"""
    asyncio.run(init_db())
    asyncio.run(_insert_eval_rows_with_per_qa("run_plain"))

    r = TestClient(app).get("/eval/result", params={"run_id": "run_plain"})

    assert r.status_code == 200
    assert "per_qa" not in r.json()


def test_eval_result_detail_no_rows():
    """detail=true 但无历史 → 空 results，无 per_qa 键"""
    asyncio.run(init_db())

    r = TestClient(app).get("/eval/result", params={"detail": "true"})

    assert r.status_code == 200
    body = r.json()
    assert body["results"] == []
    assert "per_qa" not in body


# ═══ 5. /logs ═════════════════════════════════════════════

def _make_logs_dir(tmp_path):
    """构造日志目录：a.log（3 行，最新）、b.log（2 行）、a.stderr.log（应被排除）"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "a.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (log_dir / "b.log").write_text("b1\nb2\n", encoding="utf-8")
    (log_dir / "a.stderr.log").write_text("stderr\n", encoding="utf-8")
    ConfigRegistry.override("paths.logs", str(log_dir))
    # 确定性 mtime：a.log 最新
    os.utime(log_dir / "b.log", (1_600_000_000, 1_600_000_000))
    os.utime(log_dir / "a.log", (1_600_000_100, 1_600_000_100))
    os.utime(log_dir / "a.stderr.log", (1_600_000_200, 1_600_000_200))
    return log_dir


def test_logs_list_files_and_default(tmp_path):
    """GET /logs → {files, default}：排除 .stderr.log，default=最新文件"""
    _make_logs_dir(tmp_path)

    r = TestClient(app).get("/logs")

    assert r.status_code == 200
    body = r.json()
    assert body["files"] == ["a.log", "b.log"]
    assert body["default"] == "a.log"


def test_logs_tail_file(tmp_path):
    """GET /logs?file=&lines= → {file, lines, content} 末尾 N 行"""
    _make_logs_dir(tmp_path)

    r = TestClient(app).get("/logs", params={"file": "a.log", "lines": 2})

    assert r.status_code == 200
    body = r.json()
    assert body["file"] == "a.log"
    assert body["lines"] == 2
    assert body["content"] == "line2\nline3"


def test_logs_tail_lines_clamped(tmp_path):
    """lines 超 1000 → 钳制到 1000；文件行数不足 → 全量返回"""
    _make_logs_dir(tmp_path)
    log_dir = Path(ConfigRegistry.get("paths.logs"))
    (log_dir / "big.log").write_text("\n".join(f"L{i}" for i in range(1500)), encoding="utf-8")

    r = TestClient(app).get("/logs", params={"file": "big.log", "lines": 5000})

    assert r.status_code == 200
    body = r.json()
    assert body["lines"] == 1000
    assert len(body["content"].splitlines()) == 1000


def test_logs_missing_file_404(tmp_path):
    """文件不存在 → 404"""
    _make_logs_dir(tmp_path)

    r = TestClient(app).get("/logs", params={"file": "nope.log"})

    assert r.status_code == 404


def test_logs_traversal_rejected_404(tmp_path):
    """路径穿越文件名（/、..）→ 404"""
    _make_logs_dir(tmp_path)

    for evil in ["../config.yaml", "a/b.log", "..%2E%2E%2Fetc%2Fpasswd"]:
        r = TestClient(app).get("/logs", params={"file": evil})
        assert r.status_code == 404, evil


def test_logs_list_empty_dir(tmp_path):
    """日志目录无 *.log → files=[] + default=""（不 500）"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "only.stderr.log").write_text("x\n", encoding="utf-8")
    ConfigRegistry.override("paths.logs", str(log_dir))

    r = TestClient(app).get("/logs")

    assert r.status_code == 200
    assert r.json() == {"files": [], "default": ""}


# ═══ 6. /db 浏览 ═══════════════════════════════════════════

def test_db_tables_six_tables():
    """GET /db/tables → 六表 {name, rows} 清单"""
    asyncio.run(init_db())

    r = TestClient(app).get("/db/tables")

    assert r.status_code == 200
    body = r.json()
    assert [t["name"] for t in body["tables"]] == [
        "cache_entries", "eval_history", "request_metrics",
        "turns", "sessions", "ingest_log"]
    assert all(t["rows"] == 0 for t in body["tables"])


async def _insert_cache_rows():
    """插入 2 行 cache_entries（含超长 answer）— 单事件循环内完成（aiosqlite 绑定 loop）"""
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO cache_entries (cache_key, query, answer, sources_json, "
            "token_usage, retrieval_mode) VALUES (?,?,?,?,?,?)",
            ("k1", "q", "x" * 600, "[]", 10, "hybrid+rerank"))
        await db.execute(
            "INSERT INTO cache_entries (cache_key, query, answer, sources_json, "
            "token_usage, retrieval_mode) VALUES (?,?,?,?,?,?)",
            ("k2", "q2", "y", "[]", 20, "hybrid"))
        await db.commit()
    finally:
        await db.close()


def test_db_table_rows_and_truncation(tmp_path):
    """GET /db/table/{name} → columns + rows；超长字段截断 500 字符；limit 钳制"""
    asyncio.run(init_db())
    asyncio.run(_insert_cache_rows())

    r = TestClient(app).get("/db/table/cache_entries")

    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "cache_entries"
    assert "cache_key" in body["columns"] and "answer" in body["columns"]
    assert len(body["rows"]) == 2
    row0 = dict(zip(body["columns"], body["rows"][0]))
    assert row0["cache_key"] == "k1"
    assert len(row0["answer"]) == 500          # 600 字符 → 截断 500
    assert row0["answer"] == "x" * 500
    row1 = dict(zip(body["columns"], body["rows"][1]))
    assert row1["answer"] == "y"               # 短字段原样


def test_db_table_limit_clamped():
    """limit 超 200 → 钳制到 200（不 500）"""
    asyncio.run(init_db())

    r = TestClient(app).get("/db/table/sessions", params={"limit": 9999})

    assert r.status_code == 200
    assert len(r.json()["rows"]) == 0


def test_db_table_unknown_404():
    """非白名单表名 → 404"""
    asyncio.run(init_db())

    r = TestClient(app).get("/db/table/evil_table")

    assert r.status_code == 404
