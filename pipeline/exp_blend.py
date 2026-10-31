"""精排与 RRF 分数的融合权重实验。

现象：bge-reranker-large 在"高度相关"的条款上给出 0.95~0.998 的分数，
      彼此挤在一起，排序几乎由小数第三位决定，反而把 RRF 已经排对的名次打乱。
做法：final = rerank_score + alpha * rrf_score_norm
      把 RRF 的名次信息当作先验，alpha=0 等价于纯精排，alpha 越大越信任融合排序。
产出：不同 alpha 下的 Top1/Top3，用于定参，而不是拍脑袋。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DB_BACKEND", "mysql")
os.environ["TOKENIZERS_PARALLELISM"] = "false"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import torch
torch.set_num_threads(os.cpu_count() or 4)

import common
from retrieval.bm25 import BM25
from retrieval.hybrid import HybridRetriever
from retrieval.vector_store import get_vector_index
from retrieval.rerank import Reranker
from pipeline.eval_retrieval import GOLD, hit

CACHE = ROOT / "data" / "processed" / "rerank_scores.json"


def collect(pool: int = 8, force: bool = False) -> list[dict]:
    if CACHE.exists() and not force:
        return json.loads(CACHE.read_text(encoding="utf-8"))

    docs = common.load_clauses()
    base = HybridRetriever(BM25(docs), get_vector_index())
    rr = Reranker()
    out = []
    for q, doc, no, lv in GOLD:
        cand = base.search(q, top_k=pool)
        pairs = [[q, f"《{d['doc_name']}》{d['clause_no']} {(d.get('content') or '')[:rr.content_chars]}"]
                 for d, _ in cand]
        scores = rr.model.compute_score(pairs, max_length=rr.max_length,
                                        batch_size=len(pairs), normalize=True)
        out.append({
            "q": q, "gold_doc": doc, "gold_no": no,
            "cand": [{"doc": d["doc_name"], "no": d["clause_no"],
                      "rrf": s, "rr": float(x)} for (d, s), x in zip(cand, scores)],
        })
        print(f"  {q[:18]} ok")
    CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def evaluate(data: list[dict], alpha: float, mode: str = "blend") -> tuple[int, int]:
    t1 = t3 = 0
    for row in data:
        cand = row["cand"]
        if mode == "rrf":
            ranked = sorted(cand, key=lambda c: -c["rrf"])
        elif mode == "rr":
            ranked = sorted(cand, key=lambda c: -c["rr"])
        else:
            mx = max(c["rrf"] for c in cand) or 1.0
            ranked = sorted(cand, key=lambda c: -(c["rr"] + alpha * c["rrf"] / mx))
        top = ranked[:3]
        if top and top[0]["doc"] == row["gold_doc"] and hit(top[0]["no"], row["gold_no"]):
            t1 += 1
        if any(c["doc"] == row["gold_doc"] and hit(c["no"], row["gold_no"]) for c in top):
            t3 += 1
    return t1, t3


if __name__ == "__main__":
    data = collect(force="--force" in sys.argv)
    n = len(data)
    print(f"\n样本 {n} 条，候选池 8")
    print(f"{'策略':<22}{'Top1':>10}{'Top3':>10}")
    for mode, label in [("rrf", "纯 RRF（无精排）"), ("rr", "纯精排 alpha=0")]:
        a, b = evaluate(data, 0, mode)
        print(f"{label:<22}{a:>4}/{n} {a/n*100:5.1f}%{b:>4}/{n} {b/n*100:5.1f}%")
    for alpha in (0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0):
        a, b = evaluate(data, alpha)
        print(f"{'融合 alpha=' + str(alpha):<22}{a:>4}/{n} {a/n*100:5.1f}%{b:>4}/{n} {b/n*100:5.1f}%")
