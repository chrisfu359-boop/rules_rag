"""回归评测：五项指标 + 分层报数（P10 / 任务 1）。

跑的是线上同一条链路（service.qa.answer），不是另写一套检索。
评测和线上必须是同一份代码 —— 否则测的是另一个系统，指标再好看也没意义。

离线可跑：没配 LLM key 时生成层自动走模板；没装 FlagEmbedding/BGE 时检索降级 BM25。
所以在 CI 里也能跑，而且结果确定（同一份评测集 + 同一份数据 -> 同一组数）。

五项指标（口径写死在这里，不许为了过门禁回头调）：
  1 条款引用准确率  cite   = 答案实际引用的条款号命中 gold（层级号互认，2.3 与 2.3.1 同族）
                            "实际引用"优先取结论正文里出现的编号；一句没引用就退回首条召回。
  2 时效正确率      time   = 时效题里，用户能看到的字面答复中出现标准时限（结论+提示+时限列表）
  3 高风险漏答率    l3miss = L3 题里 handoff 不为真的比例，必须 0%
  4 拒答精确率      refuse = 判定"无依据"的题里，真正库里没有答案的比例
  5 P95 延迟 + token 成本

用法：
  python eval/run_regression.py                 # 全量 300 条，写 reports/
  python eval/run_regression.py --limit 20      # 冒烟
  python eval/run_regression.py --no-write      # 只打印
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("VECTOR_BACKEND", "npz")

import common
import service.qa as qa
from generation.answer import CLAUSE_NO_RE, PLATFORM_NO_RE, _same_family
from generation.llm import get_llm
from retrieval.bm25 import BM25
from retrieval.hybrid import HybridRetriever
from retrieval.rerank import Reranker
from retrieval.vector_store import get_vector_index

EVAL_CSV = ROOT / "data" / "eval" / "eval_300.csv"
REPORTS = ROOT / "reports"
GROUPS = ("法规检索", "时效版本", "高风险拒答", "跨条款综合", "边界无答案")

METRIC_SPEC = OrderedDict([
    ("cite_accuracy", "条款引用准确率：引用号命中 gold 的比例（越高越好）"),
    ("recall_at5", "召回率：gold 出现在前 5 条的比例（越高越好）"),
    ("timeout_accuracy", "时效正确率(端到端)：全部时效题里『引用对了且说出标准时限』的比例；分母不可缩水"),
    ("timeout_consistency", "时效一致性：引用了正确条款的题里，答复写出该条款标准时限的比例（要求 100%）"),
    ("l3_miss_rate", "高风险漏答率：L3 未转人工的比例（要求 0%）"),
    ("refusal_precision", "拒答精确率：说「没依据」时真的没依据的比例"),
    ("p95_ms", "P95 单问延迟（毫秒）"),
    ("token_cost_per_query", "单问 token 成本（模板路径为 0）"),
])


def boot() -> dict:
    """按 app.py 启动钩子的顺序把链路拉起来，形状保持一致。"""
    docs = common.load_clauses()
    vindex = get_vector_index()
    if not vindex.load():
        print("[boot] 向量索引缺失，降级 BM25 单通道")
    rerank_on = os.getenv("RERANK", "on").lower() not in ("0", "off", "false")
    reranker = Reranker(enabled=rerank_on)
    if rerank_on:
        try:
            reranker.model
        except Exception as e:
            print("[boot] 精排不可用，降级原序:", str(e)[:80])
    index = HybridRetriever(BM25(docs), vindex, reranker=reranker)
    llm = get_llm()
    qa.init(docs, index, vindex, reranker, llm)
    return {"docs": docs, "vindex": vindex, "reranker": reranker, "llm": llm}


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def evaluate_row(row: dict) -> dict:
    t0 = time.time()
    res = qa.answer(row["question"])
    wall_ms = int(round((time.time() - t0) * 1000))

    concl = res.get("conclusion") or ""
    notice = res.get("notice") or ""
    visible = norm(concl + " " + notice + " " + " ".join(res.get("timeouts") or []))

    got = [c.get("clause_no") or "" for c in (res.get("clauses") or [])]
    cited = sorted(set(CLAUSE_NO_RE.findall(concl)) | set(PLATFORM_NO_RE.findall(concl)))

    gold = (row.get("gold_clause_no") or "").strip()
    if gold:
        pred = cited or got[:1]
        cite_hit = any(_same_family(n, gold) for n in pred)
        recall5 = any(_same_family(n, gold) for n in got)
    else:
        cite_hit = recall5 = None

    et = norm(row.get("expect_timeout") or "")
    timeout_hit = (et in visible) if et else None

    handoff = bool(res.get("handoff"))
    # "判定为无依据"必须是"库里确实没有"，不能把 L3 转人工也算进来 ——
    # L3 是召回成功但不给结论，算成拒答会让拒答精确率被 L3 组稀释，
    # 那个数字就没法反映"边界题会不会硬答"。
    no_basis = (not got) or ("未检索到" in concl) or ("不能据此回答" in concl)
    refused = no_basis

    # 时效拆成两个口径，缺一个就能被绕过：
    #   timeout_accuracy    端到端：分母是**全部**时效题，必须"引用对了且说出了时限"才算过。
    #                       分母不可缩水 —— 否则召回一变差，题就从分母里掉出去，
    #                       指标反而显示 100%，这正是"调指标凑数"。
    #   timeout_consistency 一致性：分母是"引用了正确条款"的题，要求 100%。
    #                       它回答的是"你说依据这条，那你有没有把这条的时限说出来"。
    timeout_ok = None
    timeout_scoped = None
    if timeout_hit is not None:
        timeout_ok = bool(cite_hit) and bool(timeout_hit)
        timeout_scoped = timeout_hit if cite_hit else None

    return {
        "id": row["id"], "group": row["group"], "question": row["question"],
        "gold": gold, "gold_doc": row.get("gold_doc") or "",
        "expect_risk": row.get("expect_risk") or "",
        "risk_level": res.get("risk_level"),
        "got": got[:3], "cited": cited,
        "cite_hit": cite_hit, "recall5": recall5,
        "timeout_hit": timeout_hit, "timeout_ok": timeout_ok,
        "timeout_scoped": timeout_scoped, "expect_timeout": et,
        "handoff": handoff, "refused": refused,
        "expect_handoff": row.get("expect_handoff") == "1",
        "generator": res.get("generator"),
        "latency_ms": int(res.get("latency_ms") or wall_ms),
    }


def pct(num: int, den: int):
    return None if not den else round(num / den, 4)


def aggregate(rows: list[dict], boot_info: dict) -> dict:
    lat = [r["latency_ms"] for r in rows]
    lat_sorted = sorted(lat)
    n = len(rows)

    def sub(items):
        cites = [r for r in items if r["cite_hit"] is not None]
        recs = [r for r in items if r["recall5"] is not None]
        tm_all = [r for r in items if r["timeout_ok"] is not None]
        tm_sc = [r for r in items if r["timeout_scoped"] is not None]
        return {
            "n": len(items),
            "cite_accuracy": pct(sum(1 for r in cites if r["cite_hit"]), len(cites)),
            "recall_at5": pct(sum(1 for r in recs if r["recall5"]), len(recs)),
            # 端到端：分母 = 全部时效题（不可缩水）
            "timeout_accuracy": pct(sum(1 for r in tm_all if r["timeout_ok"]), len(tm_all)),
            # 一致性：分母 = 引用了正确条款的时效题
            "timeout_consistency": pct(sum(1 for r in tm_sc if r["timeout_scoped"]), len(tm_sc)),
            "timeout_scoped_n": len(tm_sc),
            "handoff_rate": pct(sum(1 for r in items if r["handoff"]), len(items)),
            "avg_ms": int(round(statistics.fmean([r["latency_ms"] for r in items]))) if items else 0,
        }

    groups = OrderedDict((g, sub([r for r in rows if r["group"] == g])) for g in GROUPS)

    # 高风险层：只看"高风险拒答"组的漏答
    l3 = [r for r in rows if r["group"] == "高风险拒答"]
    l3_miss = pct(sum(1 for r in l3 if not r["handoff"]), len(l3))

    # 拒答精确率：系统说"没依据"的题里，真正属于边界组的比例
    refused = [r for r in rows if r["refused"]]
    tp = [r for r in refused if r["group"] == "边界无答案"]
    boundary = [r for r in rows if r["group"] == "边界无答案"]
    refusal_precision = pct(len(tp), len(refused))
    boundary_recall = pct(len(tp), len(boundary))

    tokens = int(getattr(boot_info["llm"], "total_tokens", 0) or 0)
    calls = int(getattr(boot_info["llm"], "calls", 0) or 0)

    overall = sub(rows)
    overall.update({
        "l3_miss_rate": l3_miss,
        "refusal_precision": refusal_precision,
        "boundary_refusal_recall": boundary_recall,
        "refused_count": len(refused),
        "p50_ms": int(round(statistics.median(lat))) if lat else 0,
        "p95_ms": lat_sorted[min(len(lat_sorted) - 1, int(n * 0.95))] if lat else 0,
        "max_ms": max(lat) if lat else 0,
        "token_total": tokens,
        "llm_calls": calls,
        "token_cost_per_query": round(tokens / n, 2) if n else 0,
    })

    failures = []
    for r in rows:
        why = []
        if r["cite_hit"] is False:
            why.append("引用未命中 gold")
        if r["timeout_scoped"] is False:
            why.append(f"引用了条款但答复缺标准时限 {r['expect_timeout']}")
        elif r["timeout_ok"] is False and r["cite_hit"] is False:
            why.append(f"未引用到含标准时限的条款（{r['expect_timeout']}）")
        if r["group"] == "高风险拒答" and not r["handoff"]:
            why.append("L3 未转人工")
        if r["group"] == "边界无答案" and not r["refused"]:
            why.append("边界题硬答")
        if why:
            failures.append({"id": r["id"], "group": r["group"], "question": r["question"],
                             "why": "；".join(why), "gold": r["gold"],
                             "got": " / ".join(r["got"]), "risk": r["risk_level"]})

    vs = boot_info["vindex"].stats()
    rs = boot_info["reranker"].stats()
    ls = boot_info["llm"].stats()
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "eval_set": str(EVAL_CSV.relative_to(ROOT)),
        "samples": n,
        "env": {
            "db_backend": common.backend(),
            "vector_backend": vs.get("backend"),
            "vector_usable": bool(vs.get("loaded")) and not getattr(boot_info["vindex"], "_enc_failed", False),
            "rerank": bool(rs.get("enabled")),
            "llm": {"model": ls.get("model"), "available": ls.get("available"),
                    "calls": calls, "tokens": tokens},
            "low_conf_threshold": qa.RT.low_conf_threshold(),
        },
        "metric_spec": METRIC_SPEC,
        "overall": overall,
        "groups": groups,
        "failures": failures,
    }


def pc(v) -> str:
    return "  -  " if v is None else f"{v * 100:.1f}%"


def print_report(d: dict) -> None:
    o = d["overall"]
    print(f"\n{'=' * 78}")
    print(f"回归评测  |  {d['samples']} 条  |  {d['generated_at']}")
    print(f"存储 {d['env']['db_backend']} / 向量 {d['env']['vector_backend']}"
          f"（可用={d['env']['vector_usable']}）/ 精排 {'on' if d['env']['rerank'] else 'off'}"
          f" / 生成 {d['env']['llm']['model'] or 'template'}")
    print(f"{'=' * 78}")
    print(f"  {'指标':<16}{'数值':>10}")
    print(f"  {'-' * 26}")
    print(f"  {'条款引用准确率':<16}{pc(o['cite_accuracy']):>10}")
    print(f"  {'召回率@5':<16}{pc(o['recall_at5']):>10}")
    print(f"  {'时效正确率':<16}{pc(o['timeout_accuracy']):>10}")
    print(f"  {'时效一致性':<16}{pc(o['timeout_consistency']):>10}")
    print(f"  {'高风险漏答率':<16}{pc(o['l3_miss_rate']):>10}")
    print(f"  {'拒答精确率':<16}{pc(o['refusal_precision']):>10}")
    print(f"  {'(边界拒答召回)':<16}{pc(o['boundary_refusal_recall']):>10}")
    print(f"  {'P50 / P95 延迟':<16}{o['p50_ms']:>5} / {o['p95_ms']:<4}ms")
    print(f"  {'token/问':<16}{o['token_cost_per_query']:>9.2f}")

    print(f"\n  {'分层':<10}{'条数':>5}{'引用准确':>10}{'召回@5':>9}{'时效':>8}{'转人工':>8}{'均耗时':>8}")
    for g, s in d["groups"].items():
        print(f"  {g:<10}{s['n']:>5}{pc(s['cite_accuracy']):>10}{pc(s['recall_at5']):>9}"
              f"{pc(s['timeout_consistency']):>8}{pc(s['handoff_rate']):>8}{s['avg_ms']:>7}ms")

    if d["failures"]:
        print(f"\n  失败 case {len(d['failures'])} 条，前 8 条：")
        for x in d["failures"][:8]:
            print(f"    [{x['id']}] {x['group']} {x['question'][:26]}")
            print(f"          {x['why']}  gold={x['gold']}  got={x['got'][:46]}")
    print(f"{'=' * 78}")


def write_outputs(d: dict) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    body = json.dumps(d, ensure_ascii=False, indent=1)
    (REPORTS / f"regression_{stamp}.json").write_text(body, encoding="utf-8")
    (REPORTS / "regression_latest.json").write_text(body, encoding="utf-8")
    # 仪表盘要能双击打开（file:// 下 fetch 被浏览器拦），所以数据做成 JS 变量
    (REPORTS / "dashboard_data.js").write_text(
        "window.REG_DATA = " + body + ";\n", encoding="utf-8")
    print(f"\n已写入 reports/regression_{stamp}.json / regression_latest.json / dashboard_data.js")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    if not EVAL_CSV.exists():
        raise SystemExit(f"缺少 {EVAL_CSV.relative_to(ROOT)}，先跑 python eval/gen_eval_set.py")
    with EVAL_CSV.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[:args.limit]
    print(f"[boot] 载入评测集 {len(rows)} 条，开始构建链路 ...")
    info = boot()
    print(f"[boot] 就绪：条款 {len(info['docs'])} 条，低置信阈值 {qa.RT.low_conf_threshold()}")

    results = []
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        results.append(evaluate_row(row))
        if i % 50 == 0:
            print(f"  ... {i}/{len(rows)}  ({round(time.time() - t0, 1)}s)")
    d = aggregate(results, info)
    print_report(d)
    if not args.no_write:
        write_outputs(d)


if __name__ == "__main__":
    main()
