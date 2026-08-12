"""L1 精确匹配缓存（CacheManager）— SQLite cache_entries 表

- cache_key = MD5("{query}|{mode}")：query 与检索模式完全一致才命中（L2 语义缓存预留）
- TTL 判定在 SQL 层完成（strftime('%s')，UTC 秒级，无时区问题）；ttl=0 即立即过期
- 写入超 max_entries 后按 created_at DESC 做 LRU 淘汰最老行
- 每次操作独立取连接（storage.sqlite_client.get_db），不持有跨请求连接
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from core.config import ConfigRegistry
from storage.sqlite_client import get_db


@dataclass
class CachedAnswer:
    answer: str
    sources: list = field(default_factory=list)
    token_usage: int = 0
    retrieval_mode: str = ""


class CacheManager:
    """L1 缓存：SQLite 持久化，精确匹配 + TTL + LRU 淘汰"""

    def __init__(self):
        self.ttl = int(ConfigRegistry.get("cache.l1.ttl", 3600))
        self.max_entries = int(ConfigRegistry.get("cache.l1.max_entries", 10000))

    def cache_key(self, query: str, mode: str) -> str:
        """MD5("{query}|{mode}") → 缓存键（不同检索模式互不干扰）"""
        return hashlib.md5(f"{query}|{mode}".encode("utf-8")).hexdigest()

    async def get(self, query: str, mode: str) -> Optional[CachedAnswer]:
        """查询缓存：未命中或已过期返回 None；过期行顺带 DELETE 清理"""
        key = self.cache_key(query, mode)
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT answer, sources_json, token_usage, retrieval_mode "
                "FROM cache_entries "
                "WHERE cache_key = ? "
                "AND strftime('%s','now') - strftime('%s', created_at) < ?",
                (key, self.ttl),
            )
            row = await cursor.fetchone()
            if row is not None:
                return CachedAnswer(
                    answer=row["answer"],
                    sources=json.loads(row["sources_json"] or "[]"),
                    token_usage=int(row["token_usage"] or 0),
                    retrieval_mode=row["retrieval_mode"] or mode,
                )
            # miss 或过期：过期行顺带删除（无过期行时影响 0 行，幂等）
            await db.execute(
                "DELETE FROM cache_entries "
                "WHERE cache_key = ? "
                "AND strftime('%s','now') - strftime('%s', created_at) >= ?",
                (key, self.ttl),
            )
            await db.commit()
            return None
        finally:
            await db.close()

    async def put(
        self, query: str, mode: str, answer: str,
        sources: List[dict], token_usage: int,
    ) -> None:
        """写入/刷新缓存（INSERT OR REPLACE）；写入后超 max_entries 执行 LRU 清理"""
        key = self.cache_key(query, mode)
        db = await get_db()
        try:
            # 显式写入微秒级 created_at，保证 LRU 排序在秒内也能确定
            await db.execute(
                "INSERT OR REPLACE INTO cache_entries "
                "(cache_key, query, answer, sources_json, token_usage, retrieval_mode, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, query, answer, json.dumps(sources, ensure_ascii=False),
                 int(token_usage or 0), mode, datetime.utcnow().isoformat()),
            )
            cursor = await db.execute("SELECT COUNT(*) AS n FROM cache_entries")
            row = await cursor.fetchone()
            if row["n"] > self.max_entries:
                await db.execute(
                    "DELETE FROM cache_entries WHERE cache_key NOT IN "
                    "(SELECT cache_key FROM cache_entries ORDER BY created_at DESC LIMIT ?)",
                    (self.max_entries,),
                )
            await db.commit()
        finally:
            await db.close()

    async def invalidate_all(self, mode: Optional[str] = None) -> None:
        """清缓存：mode=None 清全表；否则只清该 retrieval_mode（文档更新时调用）"""
        db = await get_db()
        try:
            if mode is None:
                await db.execute("DELETE FROM cache_entries")
            else:
                await db.execute(
                    "DELETE FROM cache_entries WHERE retrieval_mode = ?", (mode,))
            await db.commit()
        finally:
            await db.close()
