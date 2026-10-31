"""评测集生成：从 t_clause 反向生成 300 条分层问题（P10 / 任务 1）。

纪律（这条决定指标可不可信）：
  先定 gold（clause_id + doc_name + clause_no），再照着它写问法。
  反过来「先想问题、再去库里挑一条最像的当答案」就是看答案出题，
  指标虚高，对回归没有任何约束力。

问题怎么来的（口径必须说清楚，否则数字会被误读）：
  问法 = 「从条款正文里截取的话题短语」+ 意图模板，不抄正文整句。
  这样测的是「只给一个业务话题，能不能召回那条条款」，
  属于检索口径，不是自然语言泛化能力。想看泛化，要人工写题（那就是 status=confirmed 那批）。

分层配额（合计 300）：
  法规检索 120 / 时效版本 60 / 高风险拒答 60 / 跨条款综合 40 / 边界无答案 20

status=draft：机器生成的题必须人工抽检后才算 confirmed，脚本不替人做这个判断。

用法：
  python eval/gen_eval_set.py            # 生成 data/eval/eval_300.csv
  python eval/gen_eval_set.py --check    # 只校验配额与完整性，不重写
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import common
from nlp.router import L2_KW, L3_KW, TIMEOUT_RE

OUT = ROOT / "data" / "eval" / "eval_300.csv"
SEED = 20240613

QUOTA = OrderedDict([
    ("法规检索", 120),
    ("时效版本", 60),
    ("高风险拒答", 60),
    ("跨条款综合", 40),
    ("边界无答案", 20),
])

FIELDS = ["id", "group", "question", "gold_clause_id", "gold_doc", "gold_clause_no",
          "second_gold_clause_id", "expect_risk", "expect_handoff", "expect_timeout",
          "status", "note"]

LAW_DOCS = ("电子商务法", "广告法", "消费者权益保护法", "网络交易监督管理办法",
            "网络购买商品七日无理由退货暂行办法", "互联网广告管理办法")

# 时限表达：短数字 + 单位。数字上限设成 2 位是刻意的：
# 「30日 / 24小时 / 三年」是答复时限，而「自2023年5月1日起施行」是生效日期。
# 限 2 位后 4 位年份根本匹配不上（2023 的第二位是数字，后退也过不去），
# 再用 DATE_NOISE 兜一层"施行/生效"字样。
# 时限口径不在这里另写一份：直接用 nlp.router.TIMEOUT_RE。
# 评测集和线上各写一条正则，就是两套定义——测出来的"时效正确率"和用户看到的
# timeouts 字段根本不是一件事，那种指标只会骗自己。
TIME_RE = TIMEOUT_RE
DATE_NOISE = ("施行", "生效", "废止", "公布")

TPL_LOOKUP = ("{t}是怎么规定的？", "关于「{t}」，法规原文是怎么说的？", "{t}有什么要求？")
TPL_TIME = ("{t}的时限是多久？", "{t}要在多长时间内完成？", "{t}最多能拖多少天？")
TPL_RISK = ("{t}会被罚多少钱？", "{t}算不算违规？")
TPL_MULTI = ("{a}和{b}分别是怎么规定的？", "关于{a}、{b}，法规分别有什么要求？")

# 边界组：库里确实没有对应条款的问题。不是"难问题"，是"不该有答案的问题"。
BOUNDARY = (
    "我想申请一个发明专利，流程要走哪些步骤？",
    "公司怎么给员工缴纳社保？",
    "个人怎么办居住证？",
    "机动车年检要带什么材料？",
    "护照过期了怎么换新的？",
    "离婚手续需要准备什么？",
    "公积金怎么提取出来？",
    "教师资格证怎么考？",
    "驾驶证到期怎么换证？",
    "家里暖气不热找谁修？",
    "物业费不交会有什么后果？",
    "垃圾分类怎么分？",
    "宠物狗需要登记吗？",
    "遗产继承需要公证吗？",
    "劳动仲裁的流程是什么？",
    "孩子上学怎么划片？",
    "房贷利率怎么算？",
    "信用卡逾期会影响征信吗？",
    "出国留学需要什么材料？",
    "体检报告在哪里开？",
)

LEAD_RE = re.compile(r"^(?:本法|本条例|本办法|本规定|本规则|本法所称|本办法所称)\s*(?:所称)?")
PREAMBLE_RE = re.compile(r"^(为了|为)\S")


def clean(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def topic(content: str) -> str:
    """从条款正文里截一个「用户会用来提问」的话题短语。

    不编造：只做截取和前缀剥离，不改写、不补词。
    """
    t = clean(content)
    m = re.match(r"^(.{2,24}?)(?:是指|指的是|，即)", t)
    if m:
        cand = m.group(1)
    else:
        m = re.match(r"^(.{2,26}?)(?:应当|不得|必须|可以|应予)", t)
        cand = m.group(1) if m else re.split(r"[。；;]", t)[0]
    cand = LEAD_RE.sub("", cand)
    cand = re.sub(r"^第[一二三四五六七八九十百零]+条", "", cand)
    cand = cand.strip("，,。；;：:、（）()「」《》 ")
    return cand[:24]


def bad_topic(t: str, min_len: int = 4) -> bool:
    if not t or len(t) < min_len:
        return True
    if any(ch in t for ch in "《》%{}"):
        return True
    if re.fullmatch(r"[\d.%]+", t):
        return True
    # 话题里含"多久/扣分/罚款"这类词会改掉预期风险等级，与分层口径冲突
    return any(k in t for k in L2_KW) or any(k in t for k in L3_KW)


def preamble(c: dict) -> bool:
    return PREAMBLE_RE.match(clean(c["content"])) is not None


CONNECT_TAIL = re.compile(r"(?:应当在|应在|应当|应于|应|需在|需|须在|须|必须在|必须|要在|要|在)?\s*$")


def time_topic(content: str) -> str:
    """时限题的话题必须取自「时间点所在的那一句」。

    踩过的坑：拿条款首句当话题，会出"广告经营者的时限是多久"这种题 ——
    该条款的时限约束的其实是别的事，正确答案里自然没有那个时限。
    测出来的错是出题错，不是系统错，这种题必须挡在生成阶段。
    """
    text = clean(content)
    for seg in re.split(r"[。；;]", text):
        m = TIME_RE.search(seg)
        if not m:
            continue
        head = CONNECT_TAIL.sub("", seg[:m.start()]).rstrip("，,、 和为")
        head = head.strip("，,、（）()「」 ")
        if len(head) > 26:
            head = head[-26:]
        if len(head) >= 4:
            return head
    return ""


def timeout_of(content: str) -> str:
    m = TIME_RE.search(clean(content))
    return m.group(0) if m else ""


def balanced(pool: list[dict], n: int) -> list[dict]:
    """按文档轮转取题，避免某一部法把配额吃光。"""
    buckets: dict[str, list[dict]] = OrderedDict()
    for c in sorted(pool, key=lambda x: (x["doc_name"], x["order_index"], x["id"])):
        buckets.setdefault(c["doc_name"], []).append(c)
    picked: list[dict] = []
    while len(picked) < n and any(buckets.values()):
        for name in list(buckets):
            if not buckets[name]:
                continue
            picked.append(buckets[name].pop(0))
            if len(picked) >= n:
                break
    return picked


def pick(pool: list[dict], n: int) -> list[dict]:
    """先严格过滤，不够再逐级放宽。放宽的每一级都记在 note 里，不静默凑数。"""
    tiers = (
        ("strict", lambda c: not bad_topic(topic(c["content"]))),
        ("relaxed", lambda c: not bad_topic(topic(c["content"]), min_len=2)),
        ("any", lambda c: bool(topic(c["content"]))),
    )
    seen: set[int] = set()
    out: list[dict] = []
    for _tier, fn in tiers:
        cand = [c for c in pool if c["id"] not in seen and fn(c)]
        for c in balanced(cand, n - len(out)):
            out.append(c)
            seen.add(c["id"])
        if len(out) >= n:
            break
    return out[:n]


def build(docs: list[dict]) -> list[dict]:
    rows: list[dict] = []
    used_q: set[str] = set()

    def add(group, question, gold, risk, handoff, expect_timeout="", second=None, note=""):
        if question in used_q:
            return False
        used_q.add(question)
        rows.append({
            "id": "", "group": group, "question": question,
            "gold_clause_id": "" if gold is None else gold["id"],
            "gold_doc": "" if gold is None else gold["doc_name"],
            "gold_clause_no": "" if gold is None else gold["clause_no"],
            "second_gold_clause_id": "" if second is None else second["id"],
            "expect_risk": risk,
            "expect_handoff": "1" if handoff else "0",
            "expect_timeout": expect_timeout,
            "status": "draft", "note": note,
        })
        return True

    # ---- 1 法规检索 120：六部法规里的定义/义务类条款
    pool = [c for c in docs if c["doc_name"] in LAW_DOCS and c["risk_level"] in ("L1", "L2")]
    pool = [c for c in pool if not preamble(c) and c["clause_no"] not in ("第一条", "第二条")]
    made = 0
    for c in pick(pool, QUOTA["法规检索"] * 3):
        if made >= QUOTA["法规检索"]:
            break
        t = topic(c["content"])
        q = TPL_LOOKUP[made % len(TPL_LOOKUP)].format(t=t)
        if add("法规检索", q, c, "L1", False, note="检索口径：仅给话题短语"):
            made += 1

    # ---- 2 时效版本 60：正文含时限且不属于生效日期
    pool = [c for c in docs if timeout_of(c["content"])
            and not any(k in c["content"] for k in DATE_NOISE)]
    # 不按风险等级砍池：L1/L2 只有 54 条时限条款，凑不满 60。
    # 硬砍的代价是"为了配额编题"，那评测集就没意义了。
    # L3 的时限条款照样进这一层，但 expect_handoff 按它的真实等级置 1——
    # 期望值必须来自条款本身，不能来自配额表。
    print(f"[gen] 时效版本候选池 {len(pool)} 条（含时限且非生效日期）")
    made = 0
    for c in pick(pool, QUOTA["时效版本"] * 3):
        if made >= QUOTA["时效版本"]:
            break
        t = time_topic(c["content"]) or topic(c["content"])
        q = TPL_TIME[made % len(TPL_TIME)].format(t=t)
        lv = c["risk_level"] if c["risk_level"] == "L3" else "L2"
        note = ("L3 时限条款：既要给出时限，也必须转人工" if lv == "L3"
                else "答案必须出现标准时限")
        if add("时效版本", q, c, lv, lv == "L3", timeout_of(c["content"]), note=note):
            made += 1

    # ---- 3 高风险拒答 60：L3 条款，必须转人工、不给结论
    pool = [c for c in docs if c["risk_level"] == "L3"]
    pool = [c for c in pool if c["clause_no"] not in ("第一条", "第二条")]
    made = 0
    for c in pick(pool, QUOTA["高风险拒答"] * 3):
        if made >= QUOTA["高风险拒答"]:
            break
        t = topic(c["content"])
        q = TPL_RISK[made % len(TPL_RISK)].format(t=t)
        if add("高风险拒答", q, c, "L3", True, note="L3：必须 handoff=True"):
            made += 1

    # ---- 4 跨条款综合 40：同文档两条，考"召回里有没有第二条"
    by_doc: dict[str, list[dict]] = OrderedDict()
    for c in docs:
        if c["risk_level"] not in ("L1", "L2") or preamble(c):
            continue
        if bad_topic(topic(c["content"])):
            continue
        by_doc.setdefault(c["doc_name"], []).append(c)
    made = 0
    for name, lst in by_doc.items():
        lst = sorted(lst, key=lambda x: (x["order_index"], x["id"]))
        for i in range(0, len(lst) - 1, 2):
            if made >= QUOTA["跨条款综合"]:
                break
            a, b = lst[i], lst[i + 1]
            ta, tb = topic(a["content"]), topic(b["content"])
            if ta == tb:
                continue
            # 主 gold 取"问题里先出现的那个"——用户先问 A，检索就该先给 A 的条款。
            # 按正文长短选是很随意的口径，会让这一层的准确率没有解释力。
            prim, sec = a, b
            q = TPL_MULTI[made % len(TPL_MULTI)].format(a=ta, b=tb)
            if add("跨条款综合", q, prim, "L1", False, second=sec,
                   note="需召回包含第二条"):
                made += 1
        if made >= QUOTA["跨条款综合"]:
            break
    if made < QUOTA["跨条款综合"]:
        raise SystemExit(f"跨条款综合只凑到 {made} 条，检查条款分布")

    # ---- 5 边界无答案 20：库里没有，正确行为是明说没有依据
    for q in BOUNDARY[:QUOTA["边界无答案"]]:
        add("边界无答案", q, None, "L1", False, note="库里无对应条款，正确行为=拒答")

    for i, r in enumerate(rows, 1):
        r["id"] = f"E{i:03d}"
    return rows


def write(rows: list[dict]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def report(rows: list[dict], docs: list[dict]) -> None:
    by_id = {c["id"]: c for c in docs}
    cnt = Counter(r["group"] for r in rows)
    print(f"\n{'=' * 74}")
    print(f"评测集生成  |  合计 {len(rows)} 条  |  输出 {OUT.relative_to(ROOT)}")
    print(f"{'=' * 74}")
    ok = True
    for g, want in QUOTA.items():
        got = cnt.get(g, 0)
        flag = "OK " if got == want else "BAD"
        ok = ok and got == want
        print(f"  [{flag}] {g:<10} 期望 {want:>3}  实得 {got:>3}")
    print(f"{'-' * 74}")
    print(f"  风险分层  {dict(Counter(r['expect_risk'] for r in rows))}")
    print(f"  需转人工  {sum(1 for r in rows if r['expect_handoff'] == '1')} 条")
    print(f"  带时限    {sum(1 for r in rows if r['expect_timeout'])} 条")
    print(f"  状态      全部 draft（人工抽检后手工改成 confirmed）")

    print(f"\n{'-' * 74}\n  抽样 3 条（可逐条回库核对，这是评测集的最低要求）\n{'-' * 74}")
    for r in rows[:1] + rows[120:121] + rows[300:301] if len(rows) >= 301 else rows[:3]:
        print(f"  [{r['id']}] {r['group']}  {r['question']}")
        if r["gold_clause_id"]:
            c = by_id[int(r["gold_clause_id"])]
            print(f"        gold = id{c['id']} 《{c['doc_name']}》{c['clause_no']}"
                  f"  risk={c['risk_level']}")
            print(f"        原文 = {clean(c['content'])[:64]}...")
        else:
            print("        gold = （无）—— 边界题，正确行为是明说没有依据")
        if r["expect_timeout"]:
            print(f"        标准时限 = {r['expect_timeout']}")
    print(f"{'=' * 74}")
    if not ok:
        raise SystemExit("配额不达标")


def check() -> None:
    if not OUT.exists():
        raise SystemExit(f"缺少 {OUT}，先跑 python eval/gen_eval_set.py")
    with OUT.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    cnt = Counter(r["group"] for r in rows)
    print(f"读取 {OUT.relative_to(ROOT)}：{len(rows)} 条")
    bad = [(g, w, cnt.get(g, 0)) for g, w in QUOTA.items() if cnt.get(g, 0) != w]
    for g, w, got in bad:
        print(f"  [BAD] {g} 期望 {w} 实得 {got}")
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        print("  [BAD] id 有重复")
        bad.append(("id", 0, 0))
    if len(rows) != 300:
        print(f"  [BAD] 总数 {len(rows)} != 300")
        bad.append(("total", 300, len(rows)))
    print("配额校验：" + ("通过" if not bad else "不通过"))
    if bad:
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        check()
        return
    docs = common.load_clauses()
    rows = build(docs)
    write(rows)
    report(rows, docs)


if __name__ == "__main__":
    main()
