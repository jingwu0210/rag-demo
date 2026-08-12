"""RRF（Reciprocal Rank Fusion）融合 — 向量检索与 BM25 结果的倒数秩融合"""


class RRFFusion:
    @staticmethod
    def fuse(vec_results: list, bm25_results: list, k: int = 60) -> list:
        """RRF 融合: score = 1/(k+rank_vec) + 1/(k+rank_bm25)，k=60"""
        scores = {}
        docs = {}

        for rank, doc in enumerate(vec_results):
            cid = doc["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            docs.setdefault(cid, doc)

        for rank, doc in enumerate(bm25_results):
            cid = doc["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            docs.setdefault(cid, doc)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        result = []
        for cid, rrf_score in ranked:
            doc = dict(docs[cid])
            doc["rrf_score"] = rrf_score
            result.append(doc)
        return result
