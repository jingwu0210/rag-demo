import os
import tempfile
from unittest.mock import patch

def test_chroma_store_add_and_query():
    from storage.chroma_client import ChromaStore
    from core.config import ConfigRegistry

    with tempfile.TemporaryDirectory() as tmp:
        ConfigRegistry.init("config.yaml")
        ConfigRegistry.override("chromadb.persist_directory", tmp)

        store = ChromaStore()
        store.add(
            ids=["c1", "c2"],
            documents=["员工年假政策", "API 技术规范"],
            embeddings=[[0.1] * 16, [0.9] * 16],
            metadatas=[
                {"doc_type": "handbook", "is_active": True},
                {"doc_type": "technical", "is_active": True},
            ],
        )

        results = store.query([0.1] * 16, top_k=2)
        assert len(results) >= 1
        assert results[0]["chunk_id"] == "c1"
        assert results[0]["metadata"]["doc_type"] == "handbook"

        # metadata 过滤
        filtered = store.query([0.1] * 16, top_k=2, where={"doc_type": "technical"})
        assert len(filtered) >= 1
        assert filtered[0]["chunk_id"] == "c2"

        # 软删除（chromadb where 仅支持 metadata 字段，不能按 id 过滤，故用 doc_type 定位 c1）
        updated = store.update_metadata(where={"doc_type": "handbook"}, metadata={"is_active": False})
        assert updated == 1

def test_chroma_store_query_empty():
    from storage.chroma_client import ChromaStore
    from core.config import ConfigRegistry
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ConfigRegistry.init("config.yaml")
        ConfigRegistry.override("chromadb.persist_directory", tmp)
        store = ChromaStore()
        results = store.query([0.5] * 16, top_k=5)
        assert results == []
