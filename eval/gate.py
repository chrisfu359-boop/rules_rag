"""回归门禁：把最新评测和 baseline 比，不过就 exit 1（P10 / 任务 1）。

四条红线（写死在代码里，按需放宽就等于没有门禁）：
  1 条款引用准确率 不得比 baseline 低超过 3pp
  2 时效正确率(端到端) 不得比 baseline 低超过 3pp
     —— 它的分母是全部时效题，不随召回好坏伸缩，所以能反映"用户到底拿没拿到时限"
  3 时效一致性 必须 100%
     —— 分母是"引用了正确条款"的题：你说依据这条，就必须把这条的时限说出来
  4 高风险漏答率 必须 0%
     —— L3 漏转人工等于让 AI 替监管下罚单

为什么时效要两条而不是一条：
  只留端到端，"引用对了但没说出时限"这种错会被绝对数值淹没，看不见；
  只留一致性，召回一变差，题就从分母里掉出去，指标反而显示 100% ——
  那是最典型的"把指标调成凑数"。两条一起卡，才两头都堵住。

为什么要有 baseline 而不是只看绝对值：
  绝对值只能说明"现在多少分"，说明不了"这次改动有没有把它弄坏"。
  门禁的职责是拦住回退，不是评价水平。第一次跑没有 baseline 时只跑红线和提示 --promote。

用法：
  python eval/gate.py                      # 与 reports/baseline.json 对比
  python eval/gate.py --promote            # 通过后把本次结果定为新 baseline
  python eval/gate.py --simulate-drop 0.15 # 自测：把准确率人为打下去，验证门禁真能拦
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
LATEST = REPORTS / "regression_latest.json"
BASELINE = REPORTS / "baseline.json"

MAX_CITE_DROP = 0.03     # 3 个百分点
MIN_TIMEOUT_ACC = 1.0    # 100%
MAX_L3_MISS = 0.0        # 0%


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def pct(v) -> str:
    return "  -  " if v is None else f"{v * 100:.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--promote", action="store_true", help="把本次结果写为 baseline")
    ap.add_argument("--simulate-drop", type=float, default=0.0,
                    help="自测用：人为把引用准确率打低 N 个百分点，验证门禁能拦住")
    args = ap.parse_args()

    cur = load(LATEST)
    if not cur:
        raise SystemExit("没有 reports/regression_latest.json，先跑 python eval/run_regression.py")

    base = load(BASELINE)
    o, ob = cur["overall"], (base.get("overall") or {})

    # 环境签名不一致时，基线相对项**本来就不该比**。
    # 例：本地基线是 BM25 单通道 + 关精排跑出来的，换成装了 BGE 的环境后
    # 引用准确率会整体跳一档，此时按 3pp 卡只会误报。
    # 但两条硬红线（时效一致性 100%、L3 漏答 0%）与环境无关，照样卡。
    def sig(d):
        e = d.get("env") or {}
        return (bool(e.get("vector_usable")), bool(e.get("rerank")),
                bool((e.get("llm") or {}).get("available")))

    env_diff = bool(ob) and sig(cur) != sig(base)
    if env_diff:
        print("[gate] 环境签名与基线不一致（向量/精排/大模型开关不同）")
        print(f"       本次 {sig(cur)}　基线 {sig(base)}")
        print("       -> 跳过两项基线相对检查，只校验硬红线。换了环境请重新 --promote。")

    if args.simulate_drop:
        o = dict(o)
        o["cite_accuracy"] = round(max(0.0, (o.get("cite_accuracy") or 0.0) - args.simulate_drop), 4)
        print(f"[自测] 引用准确率被人为下调 {args.simulate_drop * 100:.0f}pp -> {pct(o['cite_accuracy'])}")

    checks = []
    cite = o.get("cite_accuracy")
    if not ob or env_diff:
        why = "无 baseline（仅记录）" if not ob else "环境签名不同（仅记录）"
        checks.append(("条款引用准确率", pct(cite), why, True))
        print("[gate] 没有 baseline，本次只校验红线。通过后执行 --promote 立基线。")
    else:
        floor = round((ob.get("cite_accuracy") or 0.0) - MAX_CITE_DROP, 4)
        ok = cite is not None and cite >= floor
        checks.append(("条款引用准确率", pct(cite), f">= {pct(floor)}（baseline {pct(ob.get('cite_accuracy'))} - {MAX_CITE_DROP * 100:.0f}pp）", ok))

    ta = o.get("timeout_accuracy")
    if not ob or env_diff:
        why = "无 baseline（仅记录）" if not ob else "环境签名不同（仅记录）"
        checks.append(("时效正确率(端到端)", pct(ta), why, True))
    else:
        tfloor = round((ob.get("timeout_accuracy") or 0.0) - MAX_CITE_DROP, 4)
        checks.append(("时效正确率(端到端)", pct(ta),
                       f">= {pct(tfloor)}（baseline {pct(ob.get('timeout_accuracy'))} - {MAX_CITE_DROP * 100:.0f}pp）",
                       ta is not None and ta >= tfloor))

    tc = o.get("timeout_consistency")
    checks.append(("时效一致性", pct(tc), f"必须 = {pct(MIN_TIMEOUT_ACC)}（分母 {o.get('timeout_scoped_n')} 题）",
                   tc is not None and tc >= MIN_TIMEOUT_ACC))

    lm = o.get("l3_miss_rate")
    checks.append(("高风险漏答率", pct(lm), f"必须 = {pct(MAX_L3_MISS)}", lm is not None and lm <= MAX_L3_MISS))

    print(f"\n{'=' * 74}")
    print(f"回归门禁  |  {cur['samples']} 条  |  评测于 {cur['generated_at']}")
    print(f"{'=' * 74}")
    print(f"  {'检查项':<18}{'当前':>9}   {'规则':<38}{'结果'}")
    print(f"  {'-' * 76}")
    failed = []
    for name, actual, rule, ok in checks:
        print(f"  {name:<18}{actual:>9}   {rule:<38}{'通过' if ok else '不通过'}")
        if not ok:
            failed.append(name)
    print(f"  {'-' * 70}")

    if failed:
        print(f"  门禁不通过：{', '.join(failed)}")
        print(f"{'=' * 74}")
        raise SystemExit(1)

    print("  门禁通过")
    print(f"{'=' * 74}")
    if args.promote:
        BASELINE.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已立基线 reports/baseline.json（{cur['generated_at']}）")
    elif not ob:
        print("\n提示：本次无 baseline，可执行 python eval/gate.py --promote 立基。")


if __name__ == "__main__":
    main()
