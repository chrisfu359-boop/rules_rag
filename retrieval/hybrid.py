"""混合检索：BM25（词法）+ BGE-M3（语义）用 RRF 融合。

为什么必须融合，而不是"向量替代 BM25"：
  实测 BM25 单通道 7 题 Top1 只有 3/7，口语换说法就崩（"能不能用最高级"
  匹配不到"不得使用国家级"）。但纯向量也有弱点——条款号、专有名词
  （"2.3.1""电子商务法第三十五条"）这类精确串，词法命中比语义更可靠。
  所以两路保留，用 RRF 融合。

为什么用 RRF 而不是加权分数相加：
  BM25 分数无上界（几十到几百），余弦相似度在 0~1。两者量纲不同，
  归一化后再加权会引入"按候选集大小漂移"的问题——这正是 EduRAG 里
  BM25 softmax 阈值那个 bug 的同源问题。RRF 只看名次不看分数，
  天然免疫量纲差异，是工业界做混合检索的默认选择（k=60 为经验值）。
"""
from __future__ import annotations

import os


def rrf_fuse(rank_lists: list[list[int]], k: int = 60, weights: list[float] | None = None
             ) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion。

    rank_lists: 每个通道的 clause_id 有序列表（名次从 0 开始）
    weights:    各通道权重，默认等权
    """
    if weights is None:
        weights = [1.0] * len(rank_lists)
    score: dict[int, float] = {}
    for w, lst in zip(weights, rank_lists):
        for rank, cid in enumerate(lst):
            score[cid] = score.get(cid, 0.0) + w / (k + rank + 1)
    return sorted(score.items(), key=lambda x: -x[1])


class HybridRetriever:
    """把 BM25 与向量两路合成一个 search 接口，签名与 BM25.search 一致。

    reranker 是可插拔的一层：有就精排（召回宽、精排窄），没有就原序返回。
    这样"评测是否开精排"可以用同一个类跑对照组，不用改两份代码。
    """

    def __init__(self, bm25, vector, bm25_weight: float | None = None,
                 vec_weight: float | None = None,
                 reranker=None, rerank_pool: int | None = None):
        self.bm25 = bm25
        self.vector = vector
        # 融合权重可用环境变量覆盖（BM25_WEIGHT / VEC_WEIGHT），默认 BM25 3.0 / 向量 0.5。
        # 标定（300 条评测集，向量+精排全开，实测）：
        #   等权 1.0/1.0 → 引用 74.6% / 召回@5 87.9%（向量等权挤掉了 BM25 已召回的 gold）
        #   3.0/0.5      → 引用 74.6% / 召回@5 89.6%（召回回升，引用不降）
        self.w = [bm25_weight if bm25_weight is not None else float(os.getenv("BM25_WEIGHT", "3.0")),
                  vec_weight if vec_weight is not None else float(os.getenv("VEC_WEIGHT", "0.5"))]
        self.reranker = reranker
        # 精排候选池大小（RERANK_POOL 可覆盖）。默认 6：只重排融合前 6 条。
        # 标定（300 条，向量+精排全开）：pool=4 召回 89.6%/引用 74.6%，
        # pool=6 召回 91.4%/引用 75.4%/拒答精确率 100%；pool=8 反而掉到 90.4%（池太大把 gold 挤出去）。
        self.rerank_pool = rerank_pool if rerank_pool is not None else int(os.getenv("RERANK_POOL", "6"))
        self.last_debug: dict = {}
        self._by_id = {d["id"]: d for d in getattr(bm25, "docs", [])}

    def search(self, query: str, top_k: int = 5, scene: str | None = None,
               recall: int = 20) -> list[tuple[dict, float]]:
        bm_hits = self.bm25.search(query, top_k=recall, scene=scene)
        by_id = {d["id"]: d for d, _ in bm_hits}

        # 三件事都成立才走向量通道：矩阵已加载 + 编码器真的能跑（查询向量要现算）。
        # 只看 mat 会在「有 npz、没装 FlagEmbedding」的机器上每条查询都抛异常。
        vec = ((getattr(self.vector, "mat", None) is not None or self.vector.load())
               and getattr(self.vector, "encoder_ready", lambda: True)())
        vec_hits = self.vector.search(query, top_k=recall, scene=scene) if vec else []

        # 向量命中的条款可能不在 BM25 候选集里，补一份完整字典
        if vec_hits:
            for cid, _ in vec_hits:
                if cid not in by_id:
                    d = self._by_id.get(cid)
                    if d:
                        by_id[cid] = d

        lists = [[d["id"] for d, _ in bm_hits], [cid for cid, _ in vec_hits]]
        fused = rrf_fuse([lst for lst in lists if lst], weights=self.w)

        self.last_debug = {
            "bm25_top": [(by_id[i]["doc_name"] + by_id[i]["clause_no"])
                         for i, _ in [(d["id"], s) for d, s in bm_hits[:3]] if i in by_id],
            "vec_top": [(i, round(s, 3)) for i, s in vec_hits[:3]],
            "channels": {"bm25": len(bm_hits), "vector": len(vec_hits)},
            "bm25_coverage": getattr(self.bm25, "last_coverage", None),
        }

        # 融合后要留够候选给精排：如果这里只取 top_k 再送给精排，
        # 精排就只能在 3 条里重新排序，Top3 永远等于 RRF 的 Top3（踩过这个坑，
        # 表现为"开了精排 Top1 涨了但 Top3 一动不动"）。
        want = max(top_k, self.rerank_pool) if self.reranker is not None else top_k
        out = []
        for cid, s in fused[:want]:
            d = by_id.get(cid)
            if d:
                out.append((d, round(s, 5)))

        # 精排层：只对融合后的前 rerank_pool 条做 cross-encoder。
        # 注意这里是"重排"不是"重召回"——精排不会捞回 RRF 没召回到的条款，
        # 所以召回率由前两路决定，精排只提升 Precision@1。
        if self.reranker is not None and len(out) > 1:
            self.last_debug["rrf_top"] = [(d["doc_name"] + d["clause_no"], s) for d, s in out[:3]]
            # pool 显式传给精排：否则 rerank() 用自身默认 pool=4，hybrid 的 rerank_pool 只影响
            # 了 want 却管不到真正重排的条数（这是"调大 rerank_pool 召回不涨"的根因）。
            return self.reranker.rerank(query, out, top_k=top_k, pool=self.rerank_pool)
        return out

    def stats(self) -> dict:
        return {"bm25_vocab": self.bm25.bm25_stats().get("vocab", 0),
                "vector": self.vector.stats(),
                "rerank": self.reranker.stats() if self.reranker is not None else {"enabled": False}}
