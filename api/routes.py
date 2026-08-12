"""API Layer — HTTP 路由（6 端点）

- /chat：请求级并发控制（guard.acquire → 429 + Retry-After）+ 硬超时（with_request_timeout → partial）
- /ingest：同步冷路径（OCR/embedding）用 run_in_threadpool 移出事件循环
- /eval/run /eval/result /report：Task 9 接入前返回 501 stub
- /health：组件探测 + 并发占用

依赖注入：服务实例一律经 request.app.state 访问（测试可整体替换），无模块级全局。
"""
from __future__ import annotations

import os
import uuid

import structlog
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.config import ConfigRegistry
from core.guard import ConcurrencyLimitExceeded
from core.logging_config import get_logger
from storage.sqlite_client import get_db

from api.schemas import ChatRequest, ChatResponseSchema, HealthResponse, IngestResponse

logger = get_logger(module="api")

router = APIRouter()

_TIMEOUT_ANSWER = "请求处理超时，请稍后重试。"


# ── 私有工具 ────────────────────────────────────────────────

async def _session_exists(session_id: str) -> bool:
    """M-3：校验 session_id 是否存在于 sessions 表（turns 有外键约束）"""
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,))
        row = await cur.fetchone()
        return row is not None
    finally:
        await db.close()


def _chat_response_from(result, session_id: str) -> ChatResponseSchema:
    """统一转换：ChatResponse dataclass 或超时 dict（guard.with_request_timeout 返回）

    注意：缓存命中时 ChatResponse.session_id 可能为 None → 归一为空串，
    与 schema 的 str 契约保持一致（"" 表示未建立会话）。
    """
    if isinstance(result, dict):
        return ChatResponseSchema(
            answer=result.get("answer", _TIMEOUT_ANSWER),
            session_id=session_id or "",
            partial=bool(result.get("partial", False)),
        )
    return ChatResponseSchema(
        answer=result.answer,
        session_id=result.session_id or "",
        sources=list(result.sources or []),
        timing_ms=dict(result.timing_ms or {}),
        token_usage=dict(result.token_usage or {}),
        refused=bool(result.refused),
        refusal_reason=result.refusal_reason,
        from_cache=bool(result.from_cache),
        partial=bool(result.partial),
    )


# ── 1. /chat ────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponseSchema)
async def chat(req: ChatRequest, request: Request):
    """问答主入口：限流（429）→ 会话校验（M-3）→ 硬超时执行"""
    chat_service = request.app.state.chat_service
    guard = request.app.state.guard
    request_id = uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(request_id=request_id)
    logger.info("chat_request_start", request_id=request_id, session_id=req.session_id)

    try:
        sem = await guard.acquire()
    except ConcurrencyLimitExceeded:
        logger.warning("chat_concurrency_limit", request_id=request_id)
        return JSONResponse(
            status_code=429,
            content={"detail": "系统繁忙，请稍后重试"},
            headers={"Retry-After": "1"},
        )

    async with sem:
        # M-3：无效 session_id（turns 外键约束）→ 创建新会话替代，避免 IntegrityError
        session_id = req.session_id
        if session_id and not await _session_exists(session_id):
            logger.warning("chat_invalid_session_recreated",
                           request_id=request_id, session_id=session_id)
            session_id = await chat_service._create_session()

        result = await guard.with_request_timeout(
            chat_service.process(req.query, session_id))

    resp = _chat_response_from(result, session_id)
    logger.info("chat_request_end", request_id=request_id,
                session_id=resp.session_id, partial=resp.partial,
                refused=resp.refused, from_cache=resp.from_cache)
    structlog.contextvars.unbind_contextvars("request_id")
    return resp


# ── 2. /ingest ──────────────────────────────────────────────

@router.post("/ingest", response_model=IngestResponse)
async def ingest(request: Request,
                 file: UploadFile = File(...),
                 doc_type: str = Form(...),
                 version: str = Form("v1.0")):
    """文档入库冷路径：保存上传文件 → run_in_threadpool 执行同步 ingest"""
    ingest_service = request.app.state.ingest_service

    corpus_dir = ConfigRegistry.get("paths.corpus", "data/corpus")
    os.makedirs(corpus_dir, exist_ok=True)
    safe_name = os.path.basename(file.filename or "upload.bin") or "upload.bin"
    dest = os.path.join(corpus_dir, safe_name)

    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)
    logger.info("ingest_file_saved", path=dest, size=len(content))

    try:
        # IngestService.ingest 为同步 CPU 密集（OCR/embedding）且内部用 asyncio.run
        # 落库 → 必须移出事件循环线程，否则阻塞 + RuntimeError
        result = await run_in_threadpool(ingest_service.ingest, dest, doc_type, version)
    except Exception as exc:
        logger.error("ingest_failed", path=dest, error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail=f"入库失败：{exc}") from exc

    return IngestResponse(
        status=result.status,
        chunks_created=result.chunks_created,
        chunks_replaced=result.chunks_replaced,
        doc_hash=result.doc_hash,
        source_file=result.source_file,
        version=result.version,
        reason=result.reason,
    )


# ── 3-5. Task 9 占位 stub ───────────────────────────────────

@router.post("/eval/run")
async def eval_run():
    raise HTTPException(status_code=501, detail="Task 9 接入 EvalService 后开放")


@router.get("/eval/result")
async def eval_result():
    raise HTTPException(status_code=501, detail="Task 9 接入 EvalService 后开放")


@router.get("/report")
async def report():
    raise HTTPException(status_code=501, detail="Task 9 接入 EvalService 后开放")


# ── 6. /health ──────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    """组件探测：chromadb / sqlite / llm；并发占用 = max - semaphore 余量"""
    components: dict = {}

    # chromadb：collection.count() 探测
    try:
        store = getattr(request.app.state, "chroma_store", None)
        if store is None:
            raise RuntimeError("chroma_store 未初始化")
        store.collection.count()
        components["chromadb"] = "ok"
    except Exception as exc:
        components["chromadb"] = "error"
        logger.warning("health_chromadb_error", error=str(exc))

    # sqlite：SELECT 1 探测
    try:
        db = await get_db()
        try:
            cur = await db.execute("SELECT 1")
            await cur.fetchone()
        finally:
            await db.close()
        components["sqlite"] = "ok"
    except Exception as exc:
        components["sqlite"] = "error"
        logger.warning("health_sqlite_error", error=str(exc))

    # llm：startup 失败标记（Embedder 等重组件加载失败）或配置缺失 → error
    if getattr(request.app.state, "startup_error", None):
        components["llm"] = "error"
    elif ConfigRegistry.get("llm.provider") and ConfigRegistry.get("llm.model"):
        components["llm"] = "ok"
    else:
        components["llm"] = "error"

    status = "ok" if all(v == "ok" for v in components.values()) else "degraded"

    # 并发占用：semaphore 余量 _value → active = max - 余量
    guard = getattr(request.app.state, "guard", None)
    max_requests = int(ConfigRegistry.get("concurrency.max_requests", 10))
    active = 0
    if guard is not None:
        try:
            sem = guard.semaphore
            active = max(0, max_requests - int(sem._value))
        except Exception:
            active = 0

    return HealthResponse(
        status=status, components=components,
        concurrency={"active": active, "max": max_requests},
    )
