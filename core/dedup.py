"""去重与冲突检测（设计文档 §5.3）

- DedupPipeline.exact_dedup: MD5(chunk.text) 精确去重（ingest 时零成本），
  重复来源记录到保留 chunk 的 metadata["duplicate_sources"]（追溯用）。
- ConflictDetector.detect: 同 heading_path + 不同文本 → 冲突告警列表。
  兼容 Chunk 对象与 ChromaStore 查询返回的 dict（{"chunk_id","text","metadata"}）两种输入。
"""
import hashlib
from typing import List

from core.chunker import Chunk


class DedupPipeline:
    """Level 1: MD5 exact-match（ingest 时跑，零成本）"""

    def exact_dedup(self, chunks: List[Chunk]) -> List[Chunk]:
        seen = {}
        unique = []
        for c in chunks:
            key = hashlib.md5(c.text.encode("utf-8")).hexdigest()
            if key not in seen:
                seen[key] = c
                unique.append(c)
            else:
                # 记录多文档出处（追溯用）：重复 chunk 的来源追加到保留 chunk 上，
                # 同时被丢弃的 chunk 也记录，便于排查
                dup_source = c.metadata.get("source_file", "")
                seen[key].metadata.setdefault("duplicate_sources", []).append(dup_source)
                c.metadata.setdefault("duplicate_sources", []).append(
                    seen[key].metadata.get("source_file", "")
                )
        return unique


class ConflictDetector:
    """同标题 + is_active + 不同内容 → 告警"""

    @staticmethod
    def _field(obj, key: str, default=None):
        """兼容 dict（ChromaStore 查询结果）与 Chunk 对象两种输入"""
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            meta = obj.get("metadata")
            if isinstance(meta, dict) and key in meta:
                return meta[key]
            return default
        if hasattr(obj, key):
            return getattr(obj, key)
        meta = getattr(obj, "metadata", None)
        if isinstance(meta, dict) and key in meta:
            return meta[key]
        return default

    def detect(self, new_chunks: List[Chunk], existing_chunks: List[dict]) -> List[dict]:
        conflicts = []
        heading_map = {}
        for ec in existing_chunks:
            if not self._field(ec, "is_active", True):
                continue
            hp = self._field(ec, "heading_path")
            if hp:
                heading_map.setdefault(hp, []).append(ec)

        for nc in new_chunks:
            hp = self._field(nc, "heading_path")
            if hp in heading_map:
                for ec in heading_map[hp]:
                    if self._field(nc, "text") != self._field(ec, "text"):
                        conflicts.append({
                            "heading_path": hp,
                            "new_chunk_id": self._field(nc, "chunk_id") or getattr(nc, "id", None),
                            "existing_chunk_id": self._field(ec, "chunk_id") or getattr(ec, "id", None),
                            "new_source": self._field(nc, "source_file", "") or "",
                            "existing_source": self._field(ec, "source_file", "") or "",
                        })
        return conflicts
