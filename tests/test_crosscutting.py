"""横切模块测试：CacheManager（L1 缓存）+ ResilienceGuard（并发/超时守卫）+ ConversationCompressor（对话压缩）

- 项目未装 pytest-asyncio：异步测试用 asyncio.run 包同步测试函数
- 缓存测试通过 override paths.sqlite 指向 tempfile，不污染真实 workspace/cache.db
- ConfigRegistry 每测试 init("config.yaml") 重置，再按需 override
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cache import CachedAnswer, CacheManager
from core.compressor import CompressedHistory, ConversationCompressor
from core.config import ConfigRegistry
from core.generator import GenerationResult
from core.guard import ConcurrencyLimitExceeded, ResilienceGuard, StageTimeoutError
from storage.sqlite_client import init_db


# ═══ 1. CacheManager ═════════════════════════════════════════

def _cache_manager(tmp_path, ttl=3600, max_entries=10000):
    """override 到 tempfile sqlite 并建表，返回 CacheManager"""
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("paths.sqlite", str(tmp_path / "cache_test.db"))
    ConfigRegistry.override("cache.l1.ttl", ttl)
    ConfigRegistry.override("cache.l1.max_entries", max_entries)
    asyncio.run(init_db())
    return CacheManager()


def test_cache_key_deterministic_and_mode_sensitive():
    ConfigRegistry.init("config.yaml")
    mgr = CacheManager()
    # 同 query+mode 幂等
    assert mgr.cache_key("年假有几天", "hybrid") == mgr.cache_key("年假有几天", "hybrid")
    # 不同 mode 键不同（互不干扰）
    assert mgr.cache_key("年假有几天", "hybrid") != mgr.cache_key("年假有几天", "vector-only")
    # 不同 query 键不同
    assert mgr.cache_key("年假有几天", "hybrid") != mgr.cache_key("年假怎么算", "hybrid")


def test_cache_put_get_roundtrip(tmp_path):
    mgr = _cache_manager(tmp_path)
    sources = [{"chunk_id": "c1", "heading_path": "员工手册/第三章", "score": 0.91}]
    asyncio.run(mgr.put("年假有几天？", "hybrid+rerank", "每年 10 天", sources, 128))
    got = asyncio.run(mgr.get("年假有几天？", "hybrid+rerank"))

    assert got is not None
    assert isinstance(got, CachedAnswer)
    assert got.answer == "每年 10 天"
    assert got.sources == sources          # sources 从 JSON 正确还原
    assert got.token_usage == 128
    assert got.retrieval_mode == "hybrid+rerank"


def test_cache_modes_independent(tmp_path):
    mgr = _cache_manager(tmp_path)
    asyncio.run(mgr.put("q", "mode-a", "answer-a", [], 1))
    asyncio.run(mgr.put("q", "mode-b", "answer-b", [], 2))

    a = asyncio.run(mgr.get("q", "mode-a"))
    b = asyncio.run(mgr.get("q", "mode-b"))
    assert a.answer == "answer-a"
    assert b.answer == "answer-b"


def test_cache_ttl_expired_returns_none(tmp_path):
    # ttl=0 → 写入后立即视为过期，get 返回 None 且过期行被清理
    mgr = _cache_manager(tmp_path, ttl=0)
    asyncio.run(mgr.put("q", "m", "answer", [], 10))
    assert asyncio.run(mgr.get("q", "m")) is None

    # 过期行已顺带删除：再 put（非过期场景下）不影响后续写入
    mgr2 = _cache_manager(tmp_path)  # ttl 恢复 3600
    assert asyncio.run(mgr2.get("q", "m")) is None


def test_cache_invalidate_by_mode(tmp_path):
    mgr = _cache_manager(tmp_path)
    asyncio.run(mgr.put("q", "mode-a", "a", [], 1))
    asyncio.run(mgr.put("q", "mode-b", "b", [], 1))

    asyncio.run(mgr.invalidate_all("mode-a"))
    assert asyncio.run(mgr.get("q", "mode-a")) is None
    assert asyncio.run(mgr.get("q", "mode-b")) is not None


def test_cache_invalidate_all(tmp_path):
    mgr = _cache_manager(tmp_path)
    asyncio.run(mgr.put("q1", "m", "a1", [], 1))
    asyncio.run(mgr.put("q2", "m", "a2", [], 1))

    asyncio.run(mgr.invalidate_all())
    assert asyncio.run(mgr.get("q1", "m")) is None
    assert asyncio.run(mgr.get("q2", "m")) is None


def test_cache_lru_eviction(tmp_path):
    mgr = _cache_manager(tmp_path, max_entries=2)
    for q in ("q1", "q2", "q3"):
        asyncio.run(mgr.put(q, "m", f"answer-{q}", [], 1))

    # 第 3 条写入触发 LRU → 最老（q1）被淘汰，q2/q3 保留
    assert asyncio.run(mgr.get("q1", "m")) is None
    assert asyncio.run(mgr.get("q2", "m")) is not None
    assert asyncio.run(mgr.get("q3", "m")) is not None


def test_cache_put_refreshes_lru_position(tmp_path):
    mgr = _cache_manager(tmp_path, max_entries=2)
    asyncio.run(mgr.put("q1", "m", "a1", [], 1))
    asyncio.run(mgr.put("q2", "m", "a2", [], 1))
    asyncio.run(mgr.put("q1", "m", "a1-new", [], 1))   # INSERT OR REPLACE 刷新位置
    asyncio.run(mgr.put("q3", "m", "a3", [], 1))       # 触发 LRU → q2 被淘汰

    assert asyncio.run(mgr.get("q2", "m")) is None
    assert asyncio.run(mgr.get("q1", "m")).answer == "a1-new"
    assert asyncio.run(mgr.get("q3", "m")) is not None


# ═══ 2. ResilienceGuard ═════════════════════════════════════

def test_guard_stage_timeout_raises():
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("retrieval.timeout", 0.1)
    guard = ResilienceGuard()

    async def scenario():
        with pytest.raises(StageTimeoutError) as exc_info:
            await guard.with_stage_timeout("retrieval", asyncio.sleep(0.5))
        assert exc_info.value.stage == "retrieval"

    asyncio.run(scenario())


def test_guard_stage_completes_within_timeout():
    ConfigRegistry.init("config.yaml")
    guard = ResilienceGuard()

    async def scenario():
        result = await guard.with_stage_timeout("retrieval", asyncio.sleep(0.01))
        assert result is None  # 正常完成原样返回

    asyncio.run(scenario())


def test_guard_request_timeout_returns_partial():
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("concurrency.request_timeout", 0.1)
    guard = ResilienceGuard()

    async def scenario():
        result = await guard.with_request_timeout(asyncio.sleep(0.5))
        assert isinstance(result, dict)
        assert result.get("partial") is True
        assert result.get("timeout") is True

    asyncio.run(scenario())


def test_guard_acquire_returns_usable_semaphore():
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("concurrency.max_requests", 1)
    guard = ResilienceGuard()

    async def scenario():
        sem = await guard.acquire()
        assert sem is guard.semaphore
        async with sem:
            assert sem.locked() is True
        assert sem.locked() is False  # 释放后可再次使用

    asyncio.run(scenario())


def test_guard_concurrency_limit_exceeded():
    ConfigRegistry.init("config.yaml")
    ConfigRegistry.override("concurrency.max_requests", 1)
    guard = ResilienceGuard()

    async def scenario():
        sem = await guard.acquire()
        lock = asyncio.Lock()
        await lock.acquire()  # 主协程先持锁：holder 拿到槽位后将被锁阻塞住

        async def holder():
            async with sem:              # 占住唯一槽位
                await lock.acquire()     # 直到主协程 release 才退出

        task = asyncio.ensure_future(holder())
        await asyncio.sleep(0)           # holder 已占槽并阻塞在锁上

        with pytest.raises(ConcurrencyLimitExceeded):
            await guard.acquire()        # 槽位已满 → 立返异常（不排队）

        lock.release()
        await task

    asyncio.run(scenario())


# ═══ 3. ConversationCompressor ═══════════════════════════════

def _turns(n):
    return [{"query": f"q{i}", "answer": f"a{i}"} for i in range(1, n + 1)]


def _fake_generator(text="早期对话摘要", exc=None):
    gen = MagicMock()
    if exc is not None:
        gen.generate = AsyncMock(side_effect=exc)
    else:
        gen.generate = AsyncMock(return_value=GenerationResult(text=text))
    return gen


def test_compress_within_limit_skips_llm():
    ConfigRegistry.init("config.yaml")
    gen = _fake_generator()
    comp = ConversationCompressor(generator=gen)

    result = asyncio.run(comp.compress(_turns(3)))
    assert isinstance(result, CompressedHistory)
    assert result.compressed is False
    assert result.summary is None
    assert result.recent_turns == _turns(3)
    gen.generate.assert_not_called()  # 3 轮以内无 LLM 调用


def test_compress_over_limit_summarizes():
    ConfigRegistry.init("config.yaml")
    gen = _fake_generator(text="早期对话摘要")
    comp = ConversationCompressor(generator=gen)

    result = asyncio.run(comp.compress(_turns(5)))
    assert result.compressed is True
    assert result.summary == "早期对话摘要"
    assert result.recent_turns == _turns(5)[-3:]   # 最近 3 轮原文
    gen.generate.assert_awaited_once()

    # 摘要 PromptContext：摘要指令 + 空 documents + 早期轮次 history
    ctx = gen.generate.await_args.args[0]
    assert ctx.question == "请将以下对话压缩为不超过100字的中文摘要"
    assert ctx.documents == []
    assert ctx.history == _turns(5)[:-3]           # 5 轮 → 前 2 轮进摘要


def test_compress_boundary_four_turns():
    ConfigRegistry.init("config.yaml")
    gen = _fake_generator()
    comp = ConversationCompressor(generator=gen)

    result = asyncio.run(comp.compress(_turns(4)))
    assert result.compressed is True
    assert len(result.recent_turns) == 3
    assert result.recent_turns == _turns(4)[-3:]
    gen.generate.assert_awaited_once()


def test_compress_degrades_on_llm_error():
    ConfigRegistry.init("config.yaml")
    gen = _fake_generator(exc=RuntimeError("llm down"))
    comp = ConversationCompressor(generator=gen)

    # LLM 异常 → 降级：不抛异常，summary=None，近轮原文仍返回
    result = asyncio.run(comp.compress(_turns(5)))
    assert result.compressed is False
    assert result.summary is None
    assert result.recent_turns == _turns(5)[-3:]
