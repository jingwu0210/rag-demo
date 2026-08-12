"""Service Layer — IngestService（文档入库冷路径）

流程：指纹查重 → OCR → 分层分片 → 双语标注 → 精确去重 → 向量化 → 版本化入库
      → 冲突检测 → BM25 快照索引重建 → ingest_log 落库 + 清缓存

同步方法：内部异步落库（ingest_log / cache.invalidate_all）用 asyncio.run 跑独立事件循环。
调用方不得在运行中的事件循环内直接调用（API 层请用 asyncio.to_thread 包裹）。
"""
from __future__ import annotations

import asyncio
import os
from typing import List, Optional

from core.bilingual import BilingualHandler
from core.cache import CacheManager
from core.chunker import Chunk, HierarchicalChunker
from core.dedup import ConflictDetector, DedupPipeline
from core.logging_config import get_logger
from core.ocr import OCRPipeline
from core.versioned import IngestResult, VersionedIngestService
from storage.sqlite_client import get_db

logger = get_logger(module="ingest")


class IngestService:
    def __init__(self, chroma_store=None, bm25_retriever=None):
        self.chroma_store = chroma_store
        self.bm25_retriever = bm25_retriever
        self._versioned: Optional[VersionedIngestService] = None
        self._embedder = None
        # 轻量组件即时构建；重组件（Embedder / ChromaStore / BM25）惰性构建
        self.ocr = OCRPipeline()
        self.chunker = HierarchicalChunker()
        self.dedup = DedupPipeline()
        self.conflict = ConflictDetector()
        self.bilingual = BilingualHandler()
        self.cache = CacheManager()

    def _get_store(self):
        if self.chroma_store is None:
            from storage.chroma_client import ChromaStore
            self.chroma_store = ChromaStore()
        return self.chroma_store

    def _get_versioned(self) -> VersionedIngestService:
        if self._versioned is None:
            self._versioned = VersionedIngestService(self._get_store())
        return self._versioned

    def _get_bm25(self):
        if self.bm25_retriever is None:
            from core.retriever import BM25Retriever
            self.bm25_retriever = BM25Retriever(self._get_store())
        return self.bm25_retriever

    def _get_embedder(self):
        if self._embedder is None:
            from core.embedder import Embedder
            self._embedder = Embedder()
        return self._embedder

    @staticmethod
    def _run_async(coro):
        """同步冷路径内的异步落库：无运行中 loop 时用 asyncio.run；已有 loop 则报错提示"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        raise RuntimeError(
            "ingest 为同步冷路径：请在事件循环外调用（API 层用 asyncio.to_thread 包裹）")

    async def _finalize(self, result: IngestResult, doc_type: str) -> None:
        """写 ingest_log + 清 L1 缓存（文档更新后缓存失效）"""
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO ingest_log (source_file, doc_type, version, doc_hash, "
                "chunks_created, chunks_replaced, status) VALUES (?,?,?,?,?,?,?)",
                (result.source_file, doc_type, result.version, result.doc_hash,
                 result.chunks_created, result.chunks_replaced, result.status))
            await db.commit()
        finally:
            await db.close()
        await self.cache.invalidate_all()

    def ingest(self, file_path: str, doc_type: str, version: str = "v1.0") -> IngestResult:
        versioned = self._get_versioned()

        # 1. SHA-256 指纹查重：同内容且生效 → skipped 早退
        doc_hash = versioned.compute_hash(file_path)
        if versioned.check_exists(doc_hash):
            logger.info("ingest_skipped_unchanged",
                        source_file=os.path.basename(file_path), doc_hash=doc_hash)
            return IngestResult(
                status="skipped", doc_hash=doc_hash,
                source_file=os.path.basename(file_path), version=version,
                reason="文档内容未变（相同 doc_hash 已入库且 is_active）")

        # 2-3. 解析 + 分层分片
        parsed = self.ocr.process(file_path)
        chunks: List[Chunk] = self.chunker.chunk(parsed)

        # 4. 双语标注（分片器已按全文语言初始化，此处兜底补齐）
        self.bilingual.tag_chunks(chunks)

        # 5. 精确去重
        chunks = self.dedup.exact_dedup(chunks)

        # 6. 向量化（真实模型调用）→ 逐 chunk 写回 embedding
        if chunks:
            embeddings = self._get_embedder().encode([c.text for c in chunks])
            for c, vec in zip(chunks, embeddings):
                c.embedding = vec.tolist() if hasattr(vec, "tolist") else vec

        # 7. 版本化入库（软下线旧版 + 写入新版）
        result = versioned.commit(chunks, file_path, doc_type, version, doc_hash)

        # 8. 冲突检测：同 heading_path 不同文本 → warning 日志
        if result.status in ("ingested", "replaced"):
            raw = self._get_store().collection.get(where={"is_active": True})
            existing = [
                {"chunk_id": cid, "text": doc, "metadata": meta or {}}
                for cid, doc, meta in zip(raw.get("ids") or [],
                                          raw.get("documents") or [],
                                          raw.get("metadatas") or [])
            ]
            conflicts = self.conflict.detect(chunks, existing)
            if conflicts:
                logger.warning("conflict_detected", count=len(conflicts),
                               samples=conflicts[:5])

        # 9. BM25 快照索引重建（新内容生效后必须重建）
        self._get_bm25().build_index()

        # 10. ingest_log 落库 + 清缓存（非致命：失败仅记 warning）
        try:
            self._run_async(self._finalize(result, doc_type))
        except Exception:
            logger.warning("ingest_finalize_failed", exc_info=True)

        return result
