"""Task 8: API Layer 测试 — FastAPI TestClient

- 不使用 `with TestClient(app)`：避免触发 startup（会真实加载 BGE-M3 等重组件），
  直接替换 app.state 的 mock 服务（依赖注入验收）
- Python 3.9 的 asyncio.Semaphore 绑定事件循环：guard mock 的 acquire 在请求
  所在 loop 内创建信号量，避免跨 loop 报错
- 异步测试用 asyncio.run 包裹（项目未装 pytest-asyncio）
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.app import app
from core.config import ConfigRegistry
from core.guard import ConcurrencyLimitExceeded, ResilienceGuard
from core.versioned import IngestResult
from services.chat import ChatResponse


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
        sources=[{"chunk_id": "c1", "heading_path": "员工手册 > 第三章", "score": 0.91}],
        timing_ms={"retrieval": 12, "rerank": 8, "generation": 50, "total": 70},
        token_usage={"prompt": 10, "completion": 20, "total": 30},
        refused=False, refusal_reason=None, from_cache=False, partial=False,
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
    assert body["sources"] == [{"chunk_id": "c1", "heading_path": "员工手册 > 第三章", "score": 0.91}]
    assert body["timing_ms"] == {"retrieval": 12, "rerank": 8, "generation": 50, "total": 70}
    assert body["token_usage"] == {"prompt": 10, "completion": 20, "total": 30}
    assert body["refused"] is False
    assert body["refusal_reason"] is None
    assert body["from_cache"] is False
    assert body["partial"] is False
    svc.process.assert_awaited_with("年假有几天？", None)


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
    svc.process.assert_awaited_with("q", "sess-ok")


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
    svc.process.assert_awaited_with("q", "new-session-abc")


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


# ═══ 4. Task 9 占位 stub ═══════════════════════════════════

def test_eval_and_report_stubs():
    """Task 9 接入前：/eval/run /eval/result /report 全部 501"""
    client = TestClient(app)
    assert client.post("/eval/run").status_code == 501
    assert client.get("/eval/result").status_code == 501
    assert client.get("/report").status_code == 501
