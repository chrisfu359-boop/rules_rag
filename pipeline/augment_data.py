"""数据增强：补齐 FAQ 同义问法 + 回填条款版本历史（P10/P11 的数据底座）。

增强的两件事，都要能拿数据说话，不做"看起来更丰富"的假动作：

  1 FAQ 同义问法（aliases）
    库里 120 条 FAQ，有 45 条没有任何同义问法。用户不会按标准问法提问，
    "我下单了什么时候能发货"和"什么时候发货"在归一化 BM25 里覆盖率差很多。
    做法：只从该条 FAQ 自己的 keywords 出发套问法框架（能不能X / X怎么弄 / 想问X），
    不引入条款、不引入答案里没有的事实 —— 增强的是"问法"，不是"答法"。
    度量：手写 12 条口语探针，增强前后各跑一遍，报 Top1 命中与通过 0.88 阈值的条数。

  2 条款版本历史
    t_clause_version 有 406 行 active、0 行 expired，"版本"这张表其实从没被用过。
    change_pipeline 跑过之后才有历史。这里只做一件事：把 schema 补齐
    （version 列 / t_clause_history / t_change_log），让版本能力真实存在。

用法：
  python pipeline/augment_data.py --dry-run    # 只看会改什么
  python pipeline/augment_data.py             # 落库并输出增强前后对比
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import common
import service.faq as faq

REPORTS = ROOT / "reports"

# 手写口语探针：(知识域, 用户会怎么问, 期望命中的 FAQ 题干片段)
PROBES = [
    ("buyer", "我下单了什么时候能发货", "什么时候发货"),
    ("buyer", "快递大概几天能送到", "几天能到"),
    ("buyer", "你们一般发哪家快递公司", "发什么快递"),
    ("buyer", "我着急能不能快点发货", "可以加急发货吗"),
    ("buyer", "物流信息好几天不更新了", "物流一直不更新怎么办"),
    ("buyer", "显示已签收但是我没拿到", "显示已签收但我没收到"),
    ("merchant", "扣分节点到底怎么算", "扣分节点有哪些"),
    ("merchant", "我店铺被判违规了要怎么申诉", "被判定违规怎么申诉"),
    ("merchant", "申诉成功了之前扣的分会退吗", "申诉成功会退分吗"),
    ("merchant", "店铺被暂停营业了怎么恢复", "店铺被暂停营业怎么恢复"),
    ("buyer", "拍下之后多久出库", "拍下什么时候出库"),
    ("buyer", "可以指定某一天再发货吗", "能不能指定某天发货"),
]

FRAMES = ("能不能{k}", "{k}怎么弄", "想问下{k}")


def measure() -> dict:
    """对 12 条探针跑 FAQ 检索，返回分层命中情况。"""
    out = {}
    first_shape = None
    for aud in ("buyer", "merchant"):
        faq.load(aud, None)
        idx = faq.index(aud, None)
        items = [(q, exp) for a, q, exp in PROBES if a == aud]
        top1 = thr = 0
        detail = []
        for q, exp in items:
            res = idx.search(q, top_k=3)
            if res and first_shape is None:
                first_shape = sorted(res[0].keys())
            top = res[0] if res else {}
            name = str(top.get("question") or "")
            score = float(top.get("score") or top.get("sim") or 0.0)
            ok = exp in name
            top1 += ok
            thr += 1 if (ok and score >= 0.88) else 0
            detail.append({"q": q, "got": name[:24], "score": round(score, 3),
                           "hit": ok, "pass88": ok and score >= 0.88})
        out[aud] = {"n": len(items), "top1": top1, "pass_thr": thr, "detail": detail}
    out["_shape"] = first_shape
    return out


def build_aliases(question: str, keywords: str, existing: str) -> str:
    """只从这条 FAQ 自己的关键词出发套问法框架，不引入新事实。"""
    cur = [a.strip() for a in (existing or "").split("|") if a.strip()]
    kws = [k.strip() for k in (keywords or "").replace(",", " ").split() if k.strip()]
    added = []
    for kw in kws[:2]:
        for tpl in FRAMES:
            cand = tpl.format(k=kw)
            if cand == question or cand in cur or cand in added:
                continue
            if len(cand) < 3 or len(cand) > 22:
                continue
            added.append(cand)
            if len(added) >= 3:
                break
        if len(added) >= 3:
            break
    return "|".join(cur + added)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    from change_pipeline import ensure_schema
    applied = ensure_schema()
    print(f"[schema] 版本/变更相关表结构就绪（{len(applied)} 条 DDL 生效或已存在）")

    print("\n[1/3] 增强前测基线")
    before = measure()
    print(f"  探针返回字段：{before.get('_shape')}")
    for aud in ("buyer", "merchant"):
        b = before[aud]
        print(f"  {aud:<9} n={b['n']:<3} Top1 命中 {b['top1']}/{b['n']}  过 0.88 阈值 {b['pass_thr']}/{b['n']}")
        for d in b["detail"]:
            print(f"      [{'OK ' if d['hit'] else 'MISS'}] {d['q']:<22} -> {d['got']:<20} sim={d['score']}")

    rows = common.fetch_all(
        "SELECT id, question, keywords, aliases FROM t_faq ORDER BY id")
    todo = []
    for r in rows:
        new = build_aliases(r["question"], r["keywords"] or "", r["aliases"] or "")
        if new != (r["aliases"] or ""):
            todo.append((r, new))

    print(f"\n[2/3] 待补同义问法 {len(todo)} 条（原有 aliases 的 {sum(1 for r in rows if (r['aliases'] or '').strip())} 条保留不动）")
    for r, new in todo[:5]:
        print(f"  #{r['id']} {r['question'][:20]:<22} + {new[len(r['aliases'] or ''):].lstrip('|')}")
    if len(todo) > 5:
        print(f"  ... 其余 {len(todo) - 5} 条同理")

    if a.dry_run:
        print("\n[dry-run] 未落库。")
        return

    for r, new in todo:
        common.execute("UPDATE t_faq SET aliases=%s, updated_at=%s WHERE id=%s",
                       (new, time.strftime("%Y-%m-%d %H:%M:%S"), r["id"]))

    print("\n[3/3] 增强后复测")
    after = measure()
    for aud in ("buyer", "merchant"):
        b, f = before[aud], after[aud]
        print(f"  {aud:<9} Top1 命中 {b['top1']}/{b['n']} -> {f['top1']}/{f['n']}　"
              f"过阈值 {b['pass_thr']}/{b['n']} -> {f['pass_thr']}/{f['n']}")
        for d0, d1 in zip(b["detail"], f["detail"]):
            if d0["got"] != d1["got"] or d0["score"] != d1["score"]:
                print(f"      [变化] {d1['q']:<22} {d0['got']}({d0['score']}) -> {d1['got']}({d1['score']})")

    tb = sum(before[x]["top1"] for x in ("buyer", "merchant"))
    ta = sum(after[x]["top1"] for x in ("buyer", "merchant"))
    nb = sum(before[x]["n"] for x in ("buyer", "merchant"))
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "augment_report.json").write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "faq_total": len(rows),
        "faq_with_alias_before": sum(1 for r in rows if (r["aliases"] or "").strip()),
        "faq_with_alias_after": sum(1 for r in rows if (r["aliases"] or "").strip()) + len(todo),
        "aliases_added": len(todo),
        "probe": {"n": nb, "top1_before": tb, "top1_after": ta,
                  "before": before, "after": after},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n合计 Top1 {tb}/{nb} -> {ta}/{nb}；报告 reports/augment_report.json")


if __name__ == "__main__":
    main()
