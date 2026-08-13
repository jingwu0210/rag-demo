"""版本化入库服务（设计文档 §5.4）

流程：SHA-256 文档指纹查重 → 事务性替换（同 source_file_stem 的 active chunk 软下线，
再写入新 chunks）。chromadb where 仅支持 metadata 字段，故软下线用
{"source_file_stem": ..., "is_active": True} 定位，不用 chunk_id。
"""
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import List

from core.chunker import Chunk
from storage.chroma_client import ChromaStore


@dataclass
class IngestResult:
    status: str              # "ingested" | "skipped" | "replaced"
    chunks_created: int = 0
    chunks_replaced: int = 0
    doc_hash: str = ""
    source_file: str = ""
    version: str = ""
    reason: str = ""         # skipped 时的原因


class VersionedIngestService:
    def __init__(self, chroma_store: ChromaStore):
        self.store = chroma_store

    def compute_hash(self, file_path: str) -> str:
        """SHA-256 文档指纹（流式读取，避免大文件整载内存）"""
        h = sha256()
        with open(file_path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        return h.hexdigest()

    @staticmethod
    def _to_effective_int(effective_date: str, now: datetime) -> int:
        """'YYYY-MM-DD' 或 None → 整数 YYYYMMDD（chromadb $gte 仅支持数值比较）"""
        date_str = effective_date or now.strftime("%Y-%m-%d")
        return int(date_str.replace("-", ""))

    def check_exists(self, doc_hash: str) -> bool:
        """同内容且仍生效的文档是否已入库（doc_hash 且 is_active）"""
        result = self.store.collection.get(
            where={"$and": [{"doc_hash": doc_hash}, {"is_active": True}]}
        )
        return bool(result["ids"])

    def commit(self, chunks: List[Chunk], file_path: str, doc_type: str,
               version: str, doc_hash: str, effective_date: str = None,
               doc_group: str = None) -> IngestResult:
        """事务性替换：内容未变 → skipped；否则旧版软下线 + 写入新版。

        返回 status: "ingested"（首次入库）/ "replaced"（替换了旧版）。
        effective_date: 文档生效日期（YYYY-MM-DD）。None 时用今天。
        doc_group: 版本族标识（同族文档新旧版本共享）。None 时用文件名 stem。
            文件名带版本后缀时（如 handbook_v1.0.pdf / handbook_v1.1.pdf）
            stem 不同会漏替换，调用方必须显式传 doc_group。
        """
        if self.check_exists(doc_hash):
            return IngestResult(
                status="skipped", doc_hash=doc_hash,
                source_file=Path(file_path).name, version=version,
                reason="文档内容未变（相同 doc_hash 已入库且 is_active）",
            )

        if any(c.embedding is None for c in chunks):
            raise ValueError("chunks 缺少 embedding：请先通过 Embedder.encode 生成向量")

        stem = Path(file_path).stem
        group = doc_group or stem
        source_name = Path(file_path).name
        now = datetime.now()

        # 4a. 旧版软下线（同 doc_group 的所有 active chunk）。
        # 注：chromadb 0.5.23 的 where 校验要求 dict 恰有一个操作符，
        # 多条件必须用 $and（仍按 metadata 字段寻址，不用 chunk_id）
        old_count = self.store.update_metadata(
            where={"$and": [{"doc_group": group}, {"is_active": True}]},
            metadata={"is_active": False, "deleted_at": now.isoformat()},
        )

        # 4b. 写入新 chunks（metadata 按任务规范全字段）
        self.store.add(
            ids=[c.chunk_id for c in chunks],
            documents=[c.text for c in chunks],
            embeddings=[c.embedding for c in chunks],
            metadatas=[
                {
                    "chunk_id": c.chunk_id,
                    "source_file": source_name,
                    "source_file_stem": stem,
                    "doc_group": group,
                    "doc_type": doc_type,
                    "version": version,
                    # chromadb $gte 只支持数值 → 存整数 YYYYMMDD
                    "effective_date": self._to_effective_int(effective_date, now),
                    "ingested_at": now.isoformat(),
                    "doc_hash": doc_hash,
                    "language": c.language,
                    "heading_path": c.heading_path,
                    "chunk_index": i,
                    "is_active": True,
                }
                for i, c in enumerate(chunks)
            ],
        )

        status = "replaced" if old_count > 0 else "ingested"
        return IngestResult(
            status=status, chunks_created=len(chunks), chunks_replaced=old_count,
            doc_hash=doc_hash, source_file=source_name, version=version,
        )
