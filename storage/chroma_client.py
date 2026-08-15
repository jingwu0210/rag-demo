import chromadb
from chromadb.config import Settings
from core.config import ConfigRegistry

class ChromaStore:
    def __init__(self):
        persist_dir = ConfigRegistry.get("chromadb.persist_directory", "assets/chroma")
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False)
        )
        self._collection = self._client.get_or_create_collection(
            name=ConfigRegistry.get("chromadb.collection_name", "knowledge_base"),
            metadata={"hnsw:space": ConfigRegistry.get("chromadb.distance_metric", "cosine")}
        )

    @property
    def collection(self):
        return self._collection

    def add(self, ids, documents, embeddings, metadatas):
        self._collection.add(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)

    def query(self, query_embeddings, top_k=20, where=None):
        kwargs = {"query_embeddings": [query_embeddings], "n_results": top_k}
        if where:
            kwargs["where"] = where
        results = self._collection.query(**kwargs)
        return self._format_results(results)

    def update_metadata(self, where, metadata):
        """批量更新 metadata（如 is_active=False 软删除），返回更新条数"""
        results = self._collection.get(where=where)
        if results["ids"]:
            for i in range(len(results["ids"])):
                updated = {**results["metadatas"][i], **metadata}
                self._collection.update(ids=[results["ids"][i]], metadatas=[updated])
            return len(results["ids"])
        return 0

    def _format_results(self, raw):
        docs = []
        if not raw["ids"] or not raw["ids"][0]:
            return docs
        for i, chunk_id in enumerate(raw["ids"][0]):
            docs.append({
                "chunk_id": chunk_id,
                "text": raw["documents"][0][i],
                "score": 1.0 - (raw["distances"][0][i] if raw.get("distances") else 0),
                "metadata": raw["metadatas"][0][i] if raw.get("metadatas") else {}
            })
        return docs
