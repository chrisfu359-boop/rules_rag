"""检索质量评测：Top1 / Top3 命中率 + 引用校验 + 延迟。

评测集的构造纪律（很重要）：
  每道题先确定"标准条款"（gold clause_no + doc_name），再写问法。
  不能先有问题再去库里找答案——那是"看答案出题"，评测结果会虚高。
  这和短剧项目里"合成集 100% vs 真实集 36%"是同一个教训：
  口径不对，指标就是自欺。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common
from retrieval.bm25 import BM25
from retrieval.hybrid import HybridRetriever

# (问题, 标准文档, 标准条款号, 期望风险等级)
GOLD = [
    ("什么叫做平台内经营者？", "电子商务法", "第九条", "L1"),
    ("网上开店需要办营业执照吗？", "电子商务法", "第十条", "L1"),
    ("平台能不能强制商家只能在他一家卖？", "电子商务法", "第三十五条", "L1"),
    ("平台可以随便改交易规则吗？", "电子商务法", "第三十四条", "L2"),
    ("七天无理由退货，退回商品的运费谁承担？", "网络购买商品七日无理由退货暂行办法", "第十八条", "L2"),
    ("七天无理由退货的七天从哪天开始算？", "网络购买商品七日无理由退货暂行办法", "第八条", "L2"),
    ("广告里能不能用最高级这个词？", "广告法", "第九条", "L2"),
    ("买家申请仅退款，我多久要处理？", "商家售后服务管理规范", "2.3", "L2"),
    ("退货退款商家签收后多久要退款？", "商家售后服务管理规范", "2.4", "L2"),
    ("换货申请我多久内要发出？", "商家售后服务管理规范", "2.5", "L2"),
    ("售后超时未处理会怎样？", "商家售后服务管理规范", "3.1", "L2"),
    ("虚假发货会扣几分？", "商家违规行为管理规则", "2.4", "L3"),
    ("店铺被扣 24 分会有什么处罚？", "商家违规行为管理规则", "4.2", "L3"),
    ("保证金被扣了要多久补足？", "商家违规行为管理规则", "5.2", "L2"),
    ("申诉有时间限制吗？", "商家违规行为管理规则", "6.1", "L2"),
    ("商品与描述不符可以退吗？", "消费者权益保护法", "第二十四条", "L2"),
    ("直播带货的广告谁负责？", "互联网广告管理办法", "第十四条", "L2"),
    ("平台要核验商家身份吗？", "网络交易监督管理办法", "第二十四条", "L2"),
]


def norm(s: str) -> str:
    return (s or "").replace(" ", "").replace("．", ".")


def hit(no: str, gold: str) -> bool:
    """平台规则是 2.3 / 2.3.1 这种层级编号，父条命中子条也算命中。"""
    a, b = norm(no), norm(gold)
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-rerank", action="store_true", help="关闭精排跑对照组")
    ap.add_argument("--vector", default=None, choices=["milvus", "npz"], help="向量后端")
    args = ap.parse_args()

    from retrieval.vector_store import get_vector_index
    from retrieval.rerank import Reranker

    docs = common.load_clauses()
    bm25 = BM25(docs)
    vi = get_vector_index(args.vector)
    rr = None if args.no_rerank else Reranker()
    if rr is not None:
        rr.model          # 预热：否则第一题会把 3 秒加载时间算进延迟
    hybrid = HybridRetriever(bm25, vi, reranker=rr)

    rows, lat = [], []
    for q, doc, no, lv in GOLD:
        t0 = time.time()
        hits = hybrid.search(q, top_k=3)
        ms = (time.time() - t0) * 1000
        lat.append(ms)
        names = [(h[0]["doc_name"], h[0]["clause_no"]) for h in hits]
        t1 = bool(names) and names[0][0] == doc and hit(names[0][1], no)
        t3 = any(d == doc and hit(n, no) for d, n in names)
        rows.append((q, doc, no, t1, t3, names[0] if names else ("-", "-"), round(ms)))

    n = len(rows)
    t1s = sum(r[3] for r in rows)
    t3s = sum(r[4] for r in rows)
    vstats = vi.stats()

    print(f"\n{'='*78}")
    print(f"检索评测  |  样本 {n} 条  |  存储 {common.backend()}/"
          f"{vstats.get('backend','?')}  |  精排 {'on' if rr else 'off'}")
    print(f"{'='*78}")
    for q, doc, no, a, b, top, ms in rows:
        flag = "Top1" if a else ("Top3" if b else "MISS")
        print(f"  {flag:5} {ms:>5}ms  {q[:26]:<28} -> {top[0]} {top[1]}")
        if not b:
            print(f"        期望: {doc} {no}")
    print(f"{'-'*78}")
    print(f"  Top1 命中  {t1s}/{n}  = {t1s/n*100:.1f}%")
    print(f"  Top3 命中  {t3s}/{n}  = {t3s/n*100:.1f}%")
    print(f"  延迟  P50 {statistics.median(lat):.0f}ms  "
          f"P95 {sorted(lat)[int(n*0.95)-1]:.0f}ms  max {max(lat):.0f}ms")
    print(f"{'='*78}\n")

    # 落盘供 /api/eval 展示：指标不能只活在文档和终端里，
    # 否则线上跑的是哪套参数、指标是多少，谁都说不清。
    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "samples": n,
        "top1": round(t1s / n, 4), "top3": round(t3s / n, 4),
        "top1_count": t1s, "top3_count": t3s,
        "p50_ms": round(statistics.median(lat)),
        "p95_ms": round(sorted(lat)[int(n * 0.95) - 1]),
        "db_backend": common.backend(),
        "vector_backend": vstats.get("backend"),
        "rerank": bool(rr),
        "misses": [{"query": q, "gold": f"{doc} {no}", "got": f"{top[0]} {top[1]}"}
                   for q, doc, no, a, b, top, ms in rows if not b],
    }
    (Path(__file__).resolve().parent.parent / "data" / "processed" /
     "eval_latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                    encoding="utf-8")


if __name__ == "__main__":
    main()
