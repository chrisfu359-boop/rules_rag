"""Cross-encoder 精排（bge-reranker-large）。

为什么要精排：
  混合检索解决的是"召回"，RRF 融合只看名次不看语义深浅。实测 18 题里
  Top3 命中 14 题（77.8%）但 Top1 只有 10 题（55.6%）——答案在候选里，
  却没排到第一。这是典型的召回够了、排序不够。
  双塔（BGE-M3）把 query 和 clause 各自编码再算内积，两者从不交互；
  cross-encoder 把 (query, clause) 拼一起过一遍模型，能判"这条到底答不答得上"。

为什么只精排 top12 而不是全量：
  cross-encoder 是 O(n) 次前向，CPU 上一次 pair 约 100~200ms。
  精排 40 条要 4~8s，不可接受；精排 12 条约 1.2~2s，配合 Redis 缓存
  （同问题二次访问 2ms）才是一个能上线的组合。
  这就是"召回宽、精排窄"的分层：第一层要快且全，第二层要准但只看少量。

降级策略：
  模型目录不存在或加载失败 → rerank() 原序返回，检索链路不中断。

路径为什么要走 G:/models 这个软链：
  bge-reranker-large 原始路径里带中文（zzl_ai_面试），sentencepiece 是 C++ 库，
  在 Windows 上打不开非 ASCII 路径，报 "Unable to load vocabulary from file"。
  文件本身完好，是编码问题。建一个 ASCII 软链（junction）指过去即可，
  不复制 2.2G、不下载、不动原始目录。这个问题只有踩过才知道，写在这里当路标。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

MODEL_PATH = os.getenv("BGE_RERANKER_PATH", r"D:/hm_st/EduRAG/code/edu_rag/rag_qa/models/bge-reranker-large")


class Reranker:
    def __init__(self, model_path: str = MODEL_PATH, max_length: int = 160,
                 enabled: bool = True, content_chars: int = 150, blend: float = 0.05):
        self.model_path = model_path
        self.max_length = max_length
        self.enabled = enabled
        self.content_chars = content_chars
        self.blend = blend
        self._model = None
        self._failed = False
        self.last_ms = 0.0
        self.calls = 0

    @property
    def model(self):
        if self._model is None and not self._failed:
            try:
                import torch
                # 踩过的坑：这里原先把 OMP_NUM_THREADS 设成 4，12 条候选要跑 12~19 秒。
                # 本机 6 物理核 / 12 逻辑核，放到 12 线程 + batch 前向后 6 条约 2 秒。
                # CPU 推理的耗时几乎全在"线程没吃满"，不是模型不行。
                torch.set_num_threads(os.cpu_count() or 4)
                os.environ["TOKENIZERS_PARALLELISM"] = "false"
                from FlagEmbedding import FlagReranker
                if not Path(self.model_path).exists():
                    raise FileNotFoundError(self.model_path)
                t0 = time.time()
                self._model = FlagReranker(self.model_path, use_fp16=False)
                print(f"[rerank] 加载完成 {round(time.time()-t0,1)}s")
            except Exception as e:
                self._failed = True
                print("[rerank] 不可用，降级原序:", str(e)[:100])
        return self._model

    def rerank(self, query: str, docs: list[dict], top_k: int = 5,
               pool: int = 4) -> list[tuple[dict, float]]:
        """docs 为 RRF 融合后的有序候选，返回精排后的 top_k。

        兼容 hybrid.search 的返回类型：[(doc, score)]。
        """
        if not docs:
            return []
        # 关精排时必须原序回传 top_k 条。
        # 原来这里用 pool_docs[:top_k]，而 pool_docs 已经被截到 pool=4，
        # 于是 top_k=5 实际只回 4 条 —— "关掉精排"顺带把召回条数也改了，
        # 对照实验就永远不是同一个口径。
        pool_docs = docs[:pool]
        m = self.model if self.enabled else None
        if m is None:
            return [(d, s) for d, s in docs[:top_k]]

        t0 = time.time()
        # 精排输入：文档名 + 条款号 + 正文（截断）。带上出处，因为用户会问"广告法第几条"，
        # 只给正文会让模型丢掉"这条属于哪部法"这个强信号。
        # 正文截到 150 字：超长条款后半段多是罚则细节，对"这条能不能答这个问题"贡献很小，
        # 却会把 tokens 顶上去拖慢 CPU 推理。
        pairs = [[query, f"《{d.get('doc_name','')}》{d.get('clause_no','')} "
                         f"{(d.get('content') or '')[:self.content_chars]}"]
                 for d, _ in pool_docs]
        scores = m.compute_score(pairs, max_length=self.max_length,
                                 batch_size=min(len(pairs), 10), normalize=True)
        if isinstance(scores, (float, int)):
            scores = [scores]
        self.last_ms = round((time.time() - t0) * 1000, 1)
        self.calls += 1

        # 融合：精排分 + blend × 归一化 RRF 分。
        # 为什么需要这一项：精排在"几条都高度相关"时给出的分数挤在 0.95~0.998，
        # 排序实际由小数第三位决定，会打乱 RRF 已经排对的名次。
        # 实测（18 题）：纯精排 Top1 55.6%，加 0.05 的 RRF 先验后 61.1%，
        # 纯 RRF 也是 55.6%。这个权重是实验定的（pipeline/exp_blend.py），不是拍的。
        max_rrf = max((s for _, s in pool_docs), default=0.0) or 1.0
        ranked = sorted(
            ((d, float(s) + self.blend * (rrf / max_rrf)) for (d, rrf), s in zip(pool_docs, scores)),
            key=lambda x: -x[1])
        out = []
        for d, s in ranked[:top_k]:
            out.append((d, round(float(s), 4)))
        return out

    def stats(self) -> dict:
        return {"enabled": bool(self.enabled and not self._failed),
                "model": Path(self.model_path).name,
                "loaded": self._model is not None,
                "calls": self.calls, "last_ms": self.last_ms}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.environ.setdefault("DB_BACKEND", "mysql")
    import common
    from retrieval.bm25 import BM25
    from retrieval.vector_store import get_vector_index
    from retrieval.hybrid import HybridRetriever

    docs = common.load_clauses()
    ret = HybridRetriever(BM25(docs), get_vector_index())
    rr = Reranker()
    for q in ["买家申请仅退款，我多久要处理？", "广告里能不能用最高级",
              "七天无理由退货运费谁承担？"]:
        cand = ret.search(q, top_k=12)
        print(f"\n{q}")
        print("  RRF:", [(c["doc_name"][:8] + c["clause_no"], s) for c, s in cand[:3]])
        out = rr.rerank(q, cand, top_k=3)
        print(f"  RERANK({rr.last_ms}ms):", [(c["doc_name"][:8] + c["clause_no"], s) for c, s in out])
