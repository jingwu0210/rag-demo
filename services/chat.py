"""Service Layer — ChatService（顶层编排）

十步流程：缓存 → 会话 → 预处理 → 历史 → 检索 → 生成 → 后处理 → 持久化
- cache.l1.enabled 门控缓存读写
- 检索/生成阶段超时由 ResilienceGuard 管理
- 生成超时 → 降级话术 + partial=True（系统固定文案，跳过后处理，仍落库 turn/metrics）
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.bilingual import BilingualHandler
from core.cache import CachedAnswer, CacheManager
from core.compressor import ConversationCompressor
from core.config import ConfigRegistry
from core.generator import Generator
from core.guard import StageTimeoutError
from core.logging_config import get_logger
from core.metadata import MetadataFilter
from core.postprocess import PostProcessor
from core.prompt import PromptContext
from services.retrieval import RetrievalService, get_guard_singleton
from storage.sqlite_client import get_db

logger = get_logger(module="chat")

_TIMEOUT_ANSWER = "请求处理超时，请稍后重试。"


@dataclass
class ChatResponse:
    answer: str
    session_id: str
    sources: List[dict] = field(default_factory=list)   # [{"chunk_id","heading_path","score"}]
    timing_ms: Dict[str, int] = field(default_factory=dict)
    token_usage: Dict[str, int] = field(default_factory=dict)
    refused: bool = False
    refusal_reason: Optional[str] = None
    from_cache: bool = False
    partial: bool = False


class ChatService:
    def __init__(self, retrieval=None, generator=None, postprocessor=None,
                 cache=None, guard=None, compressor=None):
        self.retrieval = retrieval or RetrievalService()
        self.generator = generator or Generator()
        self.postprocessor = postprocessor or PostProcessor()
        self.cache = cache or CacheManager()
        self.guard = guard or get_guard_singleton()
        self.compressor = compressor or ConversationCompressor()

    async def process(self, query: str, session_id: str = None) -> ChatResponse:
        t_start = time.perf_counter()
        cache_enabled = bool(ConfigRegistry.get("cache.l1.enabled", True))
        mode = ConfigRegistry.get("retrieval.mode", "hybrid+rerank")

        # 1. 缓存（cache.l1.enabled 时）：命中直接返回，不建会话不检索
        if cache_enabled:
            cached = await self.cache.get(query, mode)
            if cached is not None:
                return self._from_cache(cached, session_id)

        # 2. 会话：新会话 INSERT，既有会话刷新 last_active
        if session_id is None:
            session_id = await self._create_session()
        else:
            await self._touch_session(session_id)

        # 3. 预处理
        lang = BilingualHandler.detect(query)
        doc_type = MetadataFilter.classify(query)

        # 4. 历史（+ 启用压缩时超限轮次摘要）
        history, summary = await self._get_history(session_id)

        # 5. 检索（阶段超时 → 空 docs 继续，触发低置信拒答）
        timeout = False
        retrieval_output = None
        try:
            retrieval_output = await self.guard.with_stage_timeout(
                "retrieval", self.retrieval.retrieve(query, doc_type))
        except StageTimeoutError:
            timeout = True
            logger.warning("chat_retrieval_timeout", query=query)
        docs = retrieval_output.docs if retrieval_output else []
        degraded = retrieval_output.degraded if retrieval_output else False
        injection_blocked = retrieval_output.injection_blocked if retrieval_output else 0

        # 6. 拒答预检：由 PostProcessor 内部执行（第 8 步），此处跳过

        # 7. 生成（阶段超时 → 降级话术 + partial）
        partial = False
        gen_result = None
        ctx = PromptContext(question=query, documents=docs, history=history, summary=summary)
        try:
            gen_result = await self.guard.with_stage_timeout(
                "generation", self.generator.generate(ctx))
        except StageTimeoutError:
            timeout = True
            partial = True
            logger.warning("chat_generation_timeout", query=query)

        # 8. 后处理（生成超时的系统降级话术不经过拒答/脱敏）
        if gen_result is not None:
            pp = self.postprocessor.process(gen_result.text, query, retrieval_output)
            answer = pp.answer
            refused = pp.refused
            refusal_reason = pp.refusal_reason
            pii_redact_count = pp.pii_redact_count
            tokens = {
                "prompt": gen_result.token_prompt,
                "completion": gen_result.token_completion,
                "total": gen_result.token_total,
            }
        else:
            answer = _TIMEOUT_ANSWER
            refused = False
            refusal_reason = None
            pii_redact_count = 0
            tokens = {}

        sources = [
            {"chunk_id": d.chunk_id,
             "heading_path": (d.metadata or {}).get("heading_path", ""),
             "score": d.score}
            for d in docs
        ]
        timing = {
            "retrieval": (retrieval_output.timing_ms or {}).get("retrieval", 0) if retrieval_output else 0,
            "rerank": (retrieval_output.timing_ms or {}).get("rerank", 0) if retrieval_output else 0,
            "generation": (gen_result.latency_ms if gen_result else 0) or 0,
            "total": int((time.perf_counter() - t_start) * 1000),
        }

        # 9. 持久化：写缓存（未命中且非降级话术）→ turns → request_metrics
        if cache_enabled and not partial:
            await self.cache.put(query, mode, answer, sources, tokens.get("total", 0))
        request_id = uuid.uuid4().hex
        await self._save_turn(
            session_id=session_id, raw_query=query, resolved_query=query,
            query_language=lang, answer=answer, refused=refused,
            refusal_reason=refusal_reason, from_cache=False,
            retrieval_mode=mode, sources=sources, timing_ms=timing,
            token_prompt=tokens.get("prompt", 0),
            token_completion=tokens.get("completion", 0),
            token_total=tokens.get("total", 0),
        )
        await self._save_metrics(
            request_id=request_id, session_id=session_id, timing_ms=timing,
            token_usage=tokens, retrieval_mode=mode, cache_hit=False,
            refused=refused, refusal_reason=refusal_reason,
            timeout=timeout, degraded=degraded,
            pii_redact_count=pii_redact_count, injection_blocked=injection_blocked,
        )

        # 10. 组装响应
        return ChatResponse(
            answer=answer, session_id=session_id, sources=sources,
            timing_ms=timing, token_usage=tokens, refused=refused,
            refusal_reason=refusal_reason, from_cache=False, partial=partial,
        )

    # ── 私有方法（SQLite 持久化）──────────────────────────

    def _from_cache(self, cached: CachedAnswer, session_id: Optional[str]) -> ChatResponse:
        return ChatResponse(
            answer=cached.answer,
            session_id=session_id,
            sources=list(cached.sources),
            timing_ms={"retrieval": 0, "rerank": 0, "generation": 0, "total": 0},
            token_usage={"prompt": 0, "completion": 0, "total": cached.token_usage},
            from_cache=True,
        )

    async def _create_session(self) -> str:
        session_id = uuid.uuid4().hex
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO sessions (session_id) VALUES (?)", (session_id,))
            await db.commit()
        finally:
            await db.close()
        return session_id

    async def _touch_session(self, session_id: str) -> None:
        db = await get_db()
        try:
            await db.execute(
                "UPDATE sessions SET last_active = CURRENT_TIMESTAMP "
                "WHERE session_id = ?", (session_id,))
            await db.commit()
        finally:
            await db.close()

    async def _get_history(self, session_id: str):
        """最近 max_history_turns 轮 (raw_query, answer)，反转回正序；
        enable_summary 时放宽查询窗口以便压缩超限轮次。返回 (history, summary)。"""
        max_turns = int(ConfigRegistry.get("conversation.max_history_turns", 3))
        enable_summary = bool(ConfigRegistry.get("conversation.enable_summary", False))
        limit = 100 if enable_summary else max_turns
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT raw_query, answer FROM turns "
                "WHERE session_id = ? ORDER BY turn_index DESC LIMIT ?",
                (session_id, limit))
            rows = await cursor.fetchall()
        finally:
            await db.close()
        turns = [{"query": r["raw_query"], "answer": r["answer"]} for r in rows][::-1]
        summary = None
        if enable_summary and len(turns) > max_turns:
            compressed = await self.compressor.compress(turns)
            turns = compressed.recent_turns
            summary = compressed.summary
        return turns, summary

    async def _save_turn(self, *, session_id: str, raw_query: str, resolved_query: str,
                         query_language: str, answer: str, refused: bool,
                         refusal_reason: Optional[str], from_cache: bool,
                         retrieval_mode: str, sources: List[dict],
                         timing_ms: Dict[str, int], token_prompt: int,
                         token_completion: int, token_total: int) -> None:
        turn_id = uuid.uuid4().hex
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) AS n FROM turns WHERE session_id = ?", (session_id,))
            row = await cursor.fetchone()
            turn_index = row["n"]
            await db.execute(
                "INSERT INTO turns (turn_id, session_id, turn_index, raw_query, "
                "resolved_query, query_language, answer, refused, refusal_reason, "
                "from_cache, retrieval_mode, sources_json, timing_json, "
                "token_prompt, token_completion, token_total) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (turn_id, session_id, turn_index, raw_query, resolved_query,
                 query_language, answer, int(refused), refusal_reason,
                 int(from_cache), retrieval_mode,
                 json.dumps(sources, ensure_ascii=False),
                 json.dumps(timing_ms, ensure_ascii=False),
                 int(token_prompt), int(token_completion), int(token_total)))
            await db.execute(
                "UPDATE sessions SET turn_count = turn_count + 1 "
                "WHERE session_id = ?", (session_id,))
            await db.commit()
        finally:
            await db.close()

    async def _save_metrics(self, *, request_id: str, session_id: str,
                            timing_ms: Dict[str, int], token_usage: Dict[str, int],
                            retrieval_mode: str, cache_hit: bool, refused: bool,
                            refusal_reason: Optional[str], timeout: bool,
                            degraded: bool, pii_redact_count: int,
                            injection_blocked: int) -> None:
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO request_metrics (request_id, session_id, "
                "latency_retrieval, latency_rerank, latency_generation, latency_total, "
                "token_prompt, token_completion, token_total, retrieval_mode, cache_hit, "
                "refused, refusal_reason, timeout, degraded, pii_redact_count, "
                "injection_blocked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, session_id,
                 timing_ms.get("retrieval"), timing_ms.get("rerank"),
                 timing_ms.get("generation"), timing_ms.get("total"),
                 token_usage.get("prompt", 0), token_usage.get("completion", 0),
                 token_usage.get("total", 0), retrieval_mode, int(cache_hit),
                 int(refused), refusal_reason, int(timeout), int(degraded),
                 int(pii_redact_count), int(injection_blocked)))
            await db.commit()
        finally:
            await db.close()
