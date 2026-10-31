"""精排参数压测：决定 pool / max_length / 正文截断 三者组合。

目的：CPU 上跑 xlm-roberta-large 精排，要在"准确率不掉"和"延迟能看"之间取值。
"""
import os
import time

os.environ["DB_BACKEND"] = "mysql"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import torch
torch.set_num_threads(os.cpu_count() or 4)

import common
from retrieval.bm25 import BM25
from retrieval.hybrid import HybridRetriever
from retrieval.vector_store import get_vector_index

docs = common.load_clauses()
ret = HybridRetriever(BM25(docs), get_vector_index())

from FlagEmbedding import FlagReranker
m = FlagReranker(r"G:/models/bge-reranker-large", use_fp16=False)

CASES = [
    ("买家申请仅退款，我多久要处理？", "2.3.1"),
    ("七天无理由退货运费谁承担？", "第十八条"),
    ("广告里能不能用最高级", "第九条"),
]

for pool, ml, ch in [(10, 256, 300), (8, 192, 200), (6, 192, 200), (6, 160, 160), (5, 128, 120)]:
    tot, ok = 0.0, 0
    for q, exp in CASES:
        cand = ret.search(q, top_k=10)
        pairs = [[q, f"《{d['doc_name']}》{d['clause_no']} {(d['content'] or '')[:ch]}"]
                 for d, _ in cand[:pool]]
        t = time.time()
        s = m.compute_score(pairs, max_length=ml, batch_size=pool, normalize=True)
        dt = time.time() - t
        tot += dt
        best = cand[:pool][s.index(max(s))][0]
        hit = exp in (best.get("clause_no") or "")
        ok += hit
        print(f"  pool{pool} ml{ml} ch{ch} {dt:5.2f}s  {q[:14]} -> {best['clause_no']} {'OK' if hit else 'MISS'}")
    print(f"== pool{pool} ml{ml} ch{ch}: 平均 {tot/len(CASES):.2f}s, Top1 {ok}/{len(CASES)}\n")
