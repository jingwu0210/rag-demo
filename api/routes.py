"""API Layer — HTTP 路由（6 端点）

- /chat：请求级并发控制（guard.acquire → 429 + Retry-After）+ 硬超时（with_request_timeout → partial）
- /ingest：同步冷路径（OCR/embedding）用 run_in_threadpool 移出事件循环
- /eval/run /eval/result /report：评估触发/结果查询/运营报表（I-2 终审接线，原 501 stub）
- /health：组件探测 + 并发占用

依赖注入：服务实例一律经 request.app.state 访问（测试可整体替换），无模块级全局。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import time
import uuid
from pathlib import Path
from typing import List, Optional

import numpy as np
import structlog
from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from core.bilingual import BilingualHandler
from core.config import ConfigRegistry
from core.guard import ConcurrencyLimitExceeded
from core.logging_config import get_logger
from core.metadata import MetadataFilter
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


async def _history_turns(session_id: Optional[str]) -> int:
    """L2 埋点：查询会话历史轮数（chat_request_start 用）。

    埋点查询失败（如测试环境未建表）不影响主流程 → 返回 0。
    """
    if not session_id:
        return 0
    try:
        db = await get_db()
        try:
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM turns WHERE session_id = ?", (session_id,))
            row = await cur.fetchone()
            return int(row["n"]) if row else 0
        finally:
            await db.close()
    except Exception:
        return 0


def _chat_response_from(result, session_id: str, mode: str = "") -> ChatResponseSchema:
    """统一转换：ChatResponse dataclass 或超时 dict（guard.with_request_timeout 返回）

    注意：缓存命中时 ChatResponse.session_id 可能为 None → 归一为空串，
    与 schema 的 str 契约保持一致（"" 表示未建立会话）。
    mode：dataclass 路径取 result.mode（服务端实际生效值）；
    超时 dict 路径取调用方传入的请求生效模式（process 未返回，无法得知更精确值）。
    """
    if isinstance(result, dict):
        return ChatResponseSchema(
            answer=result.get("answer", _TIMEOUT_ANSWER),
            session_id=session_id or "",
            mode=mode,
            partial=bool(result.get("partial", False)),
        )
    return ChatResponseSchema(
        answer=result.answer,
        session_id=result.session_id or "",
        mode=getattr(result, "mode", "") or "",
        sources=list(result.sources or []),
        timing_ms=dict(result.timing_ms or {}),
        token_usage=dict(result.token_usage or {}),
        refused=bool(result.refused),
        refusal_reason=result.refusal_reason,
        from_cache=bool(result.from_cache),
        partial=bool(result.partial),
    )


# ── 1. /chat ────────────────────────────────────────────────

_CHAT_MODES = ("vector-only", "hybrid", "hybrid+rerank")


@router.post("/chat", response_model=ChatResponseSchema)
async def chat(req: ChatRequest, request: Request):
    """问答主入口：限流（429）→ 会话校验（M-3）→ 硬超时执行

    mode（可选）：vector-only | hybrid | hybrid+rerank；缺省 → config 全局值。
    """
    chat_service = request.app.state.chat_service
    guard = request.app.state.guard
    request_id = uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(request_id=request_id)
    if req.mode is not None and req.mode not in _CHAT_MODES:
        raise HTTPException(status_code=422, detail=f"未知检索模式：{req.mode}")
    # 请求生效模式：请求级覆盖或 config 全局值（超时 dict 路径的响应 mode 用此值）
    effective_mode = req.mode or ConfigRegistry.get("retrieval.mode", "hybrid+rerank")
    # L2 埋点: chat_request_start（query 截断 200 + 语言 + doc_type + 历史轮数）
    logger.info("chat_request_start",
                request={"id": request_id, "session_id": req.session_id},
                query={
                    "text": req.query[:200],
                    "language": BilingualHandler.detect(req.query),
                    "doc_type": MetadataFilter.classify(req.query),
                    "history_turns": await _history_turns(req.session_id),
                })

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
            chat_service.process(req.query, session_id, req.mode))

    resp = _chat_response_from(result, session_id, effective_mode)
    # L2 埋点: chat_request_end（summary 组：一次请求完整画像）
    logger.info("chat_request_end",
                request={"id": request_id, "session_id": resp.session_id},
                summary={
                    "total_latency_ms": (resp.timing_ms or {}).get("total", 0),
                    "tokens_total": (resp.token_usage or {}).get("total", 0),
                    "cache_hit": resp.from_cache,
                    "refused": resp.refused,
                    "timeout": resp.partial,
                    "degraded": False,
                })
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

    corpus_dir = ConfigRegistry.get("paths.corpus", "assets/corpus")
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


# ── 3-5. 评估触发 / 结果查询 / 运营报表（I-2 终审接线）──────

# 后台评估任务句柄（防 GC 回收导致任务中断）；完成即移除
_PENDING_EVAL_TASKS: set = set()


def _run_eval_job(chat_service, run_id: str) -> None:
    """后台线程执行三配置评估。

    注意：run_comparison 内部用 asyncio.run 驱动 chat_service.process — 不能跑在
    事件循环线程（嵌套 asyncio.run 会 RuntimeError），经 asyncio.to_thread 移出
    事件循环是必需的（与 /ingest 的 run_in_threadpool 同理）。任何失败只记日志，
    结果仍可从 GET /eval/result 查询（已有部分写入 eval_history 的记录照常返回）。
    """
    try:
        from eval.runner import run_comparison
        from eval.test_set import load_test_set
        test_set = load_test_set(ConfigRegistry.get("eval.test_set_path"))
        run_comparison(chat_service, test_set, run_id=run_id)
        logger.info("eval_run_finished", run_id=run_id)
    except Exception as exc:
        logger.error("eval_run_failed", run_id=run_id, error=str(exc), exc_info=True)


@router.post("/eval/run")
async def eval_run(request: Request):
    """触发评估：生成 run_id → 后台线程执行三配置评估 → 立即返回 202 {run_id}"""
    chat_service = getattr(request.app.state, "chat_service", None)
    if chat_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪：chat_service 未初始化")
    run_id = "eval_{}_{}".format(time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:8])
    task = asyncio.create_task(asyncio.to_thread(_run_eval_job, chat_service, run_id))
    _PENDING_EVAL_TASKS.add(task)
    task.add_done_callback(_PENDING_EVAL_TASKS.discard)
    logger.info("eval_run_started", run_id=run_id)
    return JSONResponse(status_code=202, content={"run_id": run_id})


async def _fetch_eval_history(run_id: Optional[str]) -> List[dict]:
    """查 eval_history：指定 run_id → 该 run 全部 config 行；None → 最近一次 run。"""
    db = await get_db()
    try:
        if not run_id:
            cur = await db.execute(
                "SELECT run_id FROM eval_history "
                "ORDER BY timestamp DESC, rowid DESC LIMIT 1")
            row = await cur.fetchone()
            if row is None:
                return []
            run_id = row["run_id"]
        cur = await db.execute(
            "SELECT run_id, config_name, timestamp, test_set_hash, total_qa_pairs, "
            "faithfulness, context_precision, context_recall, answer_relevancy, "
            "answer_compliance, style_consistency, refusal_appropriateness, "
            "p50_latency_ms, p95_latency_ms, avg_tokens_per_call, "
            "total_pii_redactions, total_injections_blocked "
            "FROM eval_history WHERE run_id = ? ORDER BY config_name", (run_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def _fetch_eval_per_qa(run_id: str) -> dict:
    """解析该 run 的 per_qa_results_json → {config_name: [per_qa 条目, ...]}。

    per_qa_results_json 为 runner 写入的 JSON 数组字符串；解析失败/空 → []。
    """
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT config_name, per_qa_results_json FROM eval_history WHERE run_id = ?",
            (run_id,))
        rows = await cur.fetchall()
    finally:
        await db.close()
    out: dict = {}
    for r in rows:
        raw = r["per_qa_results_json"]
        try:
            parsed = json.loads(raw) if raw else []
            out[r["config_name"]] = parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            out[r["config_name"]] = []
    return out


@router.get("/eval/result")
async def eval_result(run_id: Optional[str] = None, detail: bool = False):
    """查询评估结果：按 run_id 返回该 run 各 config 的指标 JSON；无 run_id → 最近一次。

    detail=true → 追加 per_qa 键：{config_name: [逐条明细, ...]}（解析 per_qa_results_json）。
    """
    rows = await _fetch_eval_history(run_id)
    if not rows:
        return JSONResponse(content={"run_id": run_id or None, "results": []})
    body: dict = {"run_id": rows[0]["run_id"], "results": rows}
    if detail:
        body["per_qa"] = await _fetch_eval_per_qa(rows[0]["run_id"])
    return JSONResponse(content=body)


async def _fetch_request_metrics() -> List[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM request_metrics ORDER BY id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


_REPORT_SOURCES = ("chat", "eval")


def _group_report_metrics(group: List[dict], suffix: str) -> List[tuple]:
    """单 source 分组的指标行（suffix 形如 "[chat]"；latency 聚合用 numpy.percentile）"""
    n = len(group)
    latencies = [int(r["latency_total"] or 0) for r in group]
    token_total = sum(int(r["token_total"] or 0) for r in group)
    cache_hits = sum(1 for r in group if r["cache_hit"])
    refusals = sum(1 for r in group if r["refused"])
    pii_total = sum(int(r["pii_redact_count"] or 0) for r in group)
    inj_total = sum(int(r["injection_blocked"] or 0) for r in group)
    return [
        (f"total_requests{suffix}", n),
        (f"p50_latency_ms{suffix}", int(np.percentile(latencies, 50))),
        (f"p95_latency_ms{suffix}", int(np.percentile(latencies, 95))),
        (f"total_tokens{suffix}", token_total),
        (f"avg_tokens_per_call{suffix}", round(token_total / n, 2)),
        (f"cache_hit_rate{suffix}", round(cache_hits / n, 4)),
        (f"refusal_rate{suffix}", round(refusals / n, 4)),
        (f"pii_redactions_total{suffix}", pii_total),
        (f"injections_blocked_total{suffix}", inj_total),
    ]


def _build_report_csv(rows: List[dict]) -> str:
    """request_metrics 聚合 → metric,value CSV（按 source 分组，metric 名带 [chat]/[eval] 后缀）。

    每 metric 拆两行（如 p50_latency_ms[chat] 与 p50_latency_ms[eval]），chat/eval 分开统计，
    评估跑批不再污染用户对话报表。旧库行（无 source 字段）一律归入 chat 组。
    无数据 → 仅表头。
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["metric", "value"])
    if not rows:
        return out.getvalue()
    for source in _REPORT_SOURCES:
        group = [r for r in rows if (r.get("source") or "chat") == source]
        if group:
            writer.writerows(_group_report_metrics(group, f"[{source}]"))
    return out.getvalue()


@router.get("/report")
async def report():
    """运营报表：request_metrics 聚合 → CSV（text/csv），供下游/看板消费"""
    rows = await _fetch_request_metrics()
    return Response(content=_build_report_csv(rows), media_type="text/csv")


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


# ── 7. /config + 演示 UI ─────────────────────────────────

@router.get("/config")
async def get_config():
    """配置查看：返回 config.yaml 解析后的 dict（含 RAG_ 环境变量覆盖）。

    演示服务无鉴权，全量返回；演示 UI 的 Config 页渲染为分组折叠的 YAML 树。
    """
    registry = ConfigRegistry._instance
    if registry is None:
        registry = ConfigRegistry.init("config.yaml")
    return JSONResponse(content=registry._data)


_DEMO_UI = Path(__file__).resolve().parent / "static" / "rag-gen-ai-service-demo.html"


@router.get("/rag-gen-ai-service-demo")
async def demo_ui():
    """演示 UI：无框架单文件工作台（Chat / Ingest / Eval / Report / Maintenance / Config）"""
    if not _DEMO_UI.exists():
        raise HTTPException(
            status_code=404,
            detail="演示 UI 文件缺失：api/static/rag-gen-ai-service-demo.html")
    return FileResponse(_DEMO_UI, media_type="text/html")


# ── 8. 日志查看（演示 UI Maintenance > Logs）─────────────

def _logs_dir() -> Path:
    return Path(ConfigRegistry.get("paths.logs", "workspace/logs"))


def _tail_log(path: Path, n: int) -> List[str]:
    """读取文件末尾 n 行（splitlines 去行尾换行，无效 UTF-8 用替换符兜底）"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()[-n:]


@router.get("/logs")
async def logs(file: Optional[str] = None, lines: int = 200):
    """日志查看：无参 → {files, default}（*.log 排除 *.stderr.log，default=最新）；
    ?file=<name>&lines=N → {file, lines, content}（N 上限 1000）。
    安全：文件名拒绝 / \\ .. 与隐藏文件（防路径穿越）；文件不存在 → 404。"""
    log_dir = _logs_dir()
    if file is None:
        files = sorted(
            (p.name for p in log_dir.glob("*.log")
             if not p.name.endswith(".stderr.log")),
            key=lambda n: log_dir.joinpath(n).stat().st_mtime,
            reverse=True)
        return {"files": files, "default": files[0] if files else ""}
    if ("/" in file or "\\" in file or ".." in file
            or file.startswith(".") or not file.endswith(".log")):
        raise HTTPException(status_code=404, detail=f"日志文件不存在：{file}")
    path = log_dir / file
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"日志文件不存在：{file}")
    max_lines = min(max(lines, 1), 1000)
    content = _tail_log(path, max_lines)
    return {"file": file, "lines": len(content), "content": "\n".join(content)}


# ── 9. 数据库浏览（演示 UI Maintenance > DB）──────────────

# 表名白名单：仅六张业务表可浏览，杜绝任意表名注入
_DB_TABLES = ("cache_entries", "eval_history", "request_metrics",
              "turns", "sessions", "ingest_log")
_MAX_DB_ROWS = 200
_MAX_CELL_CHARS = 500


def _truncate_cell(v):
    """字段截断：字符串超 500 字符 → 前 500 字符（响应体积与前端渲染保护）"""
    if isinstance(v, str) and len(v) > _MAX_CELL_CHARS:
        return v[:_MAX_CELL_CHARS]
    return v


def _source_filter_sql(info, source: Optional[str]):
    """构建 source 等值过滤 SQL：(where_sql, params)。

    与 db_table 的 source 过滤逻辑一致：仅含 source 列的表生效（参数化等值匹配，
    无注入风险）；无 source 列的表（cache_entries/sessions/ingest_log 等）忽略 → 返回全部。
    """
    if not source or not any(r["name"] == "source" for r in info):
        return "", []
    return " WHERE source = ?", [source]


@router.get("/db/tables")
async def db_tables(source: Optional[str] = None):
    """表清单：{tables: [{name, rows, has_source}, ...]}（rows=行数；表缺失视为 0，不中断）。

    source（可选）：'chat'/'eval'，仅对含 source 列的表（request_metrics / turns）按 source
    等值过滤统计行数；无 source 列的表（cache_entries/eval_history/sessions/ingest_log）
    忽略该参数返回全表行数（"全部"视角）。has_source 供前端判断"行数不分来源"的弱化提示。"""
    db = await get_db()
    try:
        tables = []
        for name in _DB_TABLES:
            try:
                cur = await db.execute(f"PRAGMA table_info({name})")
                info = await cur.fetchall()
                has_source = any(r["name"] == "source" for r in info)
                where_sql, params = _source_filter_sql(info, source)
                cur = await db.execute(
                    f"SELECT COUNT(*) AS n FROM {name}{where_sql}", params)
                row = await cur.fetchone()
                tables.append({"name": name, "rows": int(row["n"]),
                               "has_source": has_source})
            except Exception:
                tables.append({"name": name, "rows": 0, "has_source": False})
        return {"tables": tables}
    finally:
        await db.close()


def _build_where_like(info, q: Optional[str]):
    """q 过滤：仅对 TEXT 类型列构造 `col LIKE ?`（参数 %q%，包含匹配）。

    info 为 PRAGMA table_info 行（r["name"] / r["type"]）；q 为空或无 TEXT 列 → 不过滤。
    返回 (where_sql, params)。
    """
    if not q:
        return "", []
    text_cols = [r["name"] for r in info
                 if str(r["type"] or "").upper() == "TEXT"]
    if not text_cols:
        return "", []
    where = " WHERE " + " OR ".join(f"{c} LIKE ?" for c in text_cols)
    return where, [f"%{q}%"] * len(text_cols)


@router.get("/db/table/{table_name}")
async def db_table(table_name: str, limit: int = 50, offset: int = 0,
                   q: Optional[str] = None, source: Optional[str] = None):
    """表数据预览：{name, columns, rows, total}（rows=值数组行，limit≤200，字段截断 500 字符）。

    rows 按 rowid 降序（最新在前）；offset 分页（offset≥0）；q 对 TEXT 列 LIKE 包含过滤，
    total=过滤后总行数（与分页联动）。表名不在白名单 → 404。
    source（可选）：'chat'/'eval'，仅对含 source 列的表生效（如 request_metrics / turns），
    按 source 列等值过滤；无 source 列的表忽略该参数返回全部（"全部"视角）。"""
    if table_name not in _DB_TABLES:
        raise HTTPException(status_code=404, detail=f"未知表：{table_name}")
    n = min(max(limit, 1), _MAX_DB_ROWS)
    offset = max(offset, 0)
    db = await get_db()
    try:
        cur = await db.execute(f"PRAGMA table_info({table_name})")
        info = await cur.fetchall()
        columns = [r["name"] for r in info]
        where_sql, params = _build_where_like(info, q)
        # source 过滤：仅含 source 列的表生效（参数化等值匹配，无注入风险）；
        # 无 source 列的表（cache_entries/sessions/ingest_log 等）忽略 → 返回全部
        if source is not None and any(r["name"] == "source" for r in info):
            if where_sql:
                where_sql += " AND source = ?"
            else:
                where_sql = " WHERE source = ?"
            params.append(source)
        cur = await db.execute(
            f"SELECT COUNT(*) AS n FROM {table_name}{where_sql}", params)
        row = await cur.fetchone()
        total = int(row["n"])
        cur = await db.execute(
            f"SELECT * FROM {table_name}{where_sql} "
            f"ORDER BY rowid DESC LIMIT ? OFFSET ?", params + [n, offset])
        rows = [[_truncate_cell(v) for v in row] for row in await cur.fetchall()]
    finally:
        await db.close()
    return {"name": table_name, "columns": columns, "rows": rows, "total": total}
