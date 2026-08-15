"""RRF（Reciprocal Rank Fusion）融合 — 向量检索与 BM25 结果的倒数秩融合"""


class RRFFusion:
    @staticmethod
    def fuse(vec_results: list, bm25_results: list, k: int = 60,
             vector_weight: float = 1.0, bm25_weight: float = 1.0) -> list:
        """加权 RRF 融合: score = w_v/(k+rank_vec) + w_b/(k+rank_bm25)，k=60

        - 默认 w_v=w_b=1.0 与经典 RRF 等价（P1h 二次融合等调用方不受影响）
        - P2a: bm25_weight<1 软降权 BM25 命中（保留术语强命中机会，不硬剔除）
        - 结果附加 vec_sim：向量路命中时填余弦分，纯 BM25 命中为 None
          （P2c 过滤依据：vec_sim 缺失视为 0 被滤）
        """
        scores = {}
        docs = {}
        vec_sims = {}
        bm25_ranks = {}

        for rank, doc in enumerate(vec_results):
            cid = doc["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + vector_weight / (k + rank + 1)
            docs.setdefault(cid, doc)
            vec_sims[cid] = float(doc["score"])

        for rank, doc in enumerate(bm25_results):
            cid = doc["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + bm25_weight / (k + rank + 1)
            docs.setdefault(cid, doc)
            # P2d: 记录 BM25 排名（1-based）供强命中豁免——仅分数>0 的真命中
            # 记录（0 分伪命中如语料小于 top_k 时整库入选，不豁免）
            if float(doc.get("score", 0)) > 0:
                bm25_ranks[cid] = rank + 1

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        result = []
        for cid, rrf_score in ranked:
            doc = dict(docs[cid])
            doc["rrf_score"] = rrf_score
            doc["vec_sim"] = vec_sims.get(cid)
            doc["bm25_rank"] = bm25_ranks.get(cid)
            result.append(doc)
        return result
