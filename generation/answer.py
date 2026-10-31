"""答案组装与引用校验。

两条生成路径，共用同一套引用校验闸门：
  1. compose()     模板抽取 —— 不依赖任何外部服务，引用 100% 可核对，语言生硬。
  2. compose_llm() 大模型生成 —— 语言自然，但必须过引用校验，编造条款号就退回模板。

为什么模板路径要保留而不是删掉：
  LLM 是外部依赖，会超时、会限流、会欠费。合规问答不能因为模型挂了就变成 500。
  降级不是"兜底凑合"，而是"降级后答案依然可核对、可交付"。
"""
from __future__ import annotations

import re

from generation import llm as llm_mod
from nlp.router import extract_timeouts

CLAUSE_NO_RE = re.compile(r"第[一二三四五六七八九十百零]+条")
# 平台规则是层级编号：2.3 / 2.3.1 / 4.2 全都真实存在。
# 原来只匹配三段式（{2}），两段式的 2.3、4.2 根本没被抽出来 ——
# 于是"编造一个两段编号"永远不会被闸门拦住，校验等于形同虚设。
# 用 (?![\d.%]) 排除小数与百分号（3.5% / 0.88），避免把业务数字误判成条款号。
PLATFORM_NO_RE = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d{1,2}){1,2})(?![\d.%])")


def _norm_no(s: str) -> str:
    return (s or "").replace(" ", "").replace("．", ".")


def _same_family(a: str, b: str) -> bool:
    """平台规则的层级编号互认：2.3 与 2.3.1 属于同一族。

    只看等值会把「模型写 2.3、召回的是 2.3.1」判成编造 —— 那是假阳性；
    而真正编造的 9.9 依然对不上任何一族，闸门不会被削弱。
    """
    a, b = _norm_no(a), _norm_no(b)
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def verify_citations(answer: str, clauses: list[dict]) -> tuple[bool, list[str]]:
    """引用校验：**只校验生成结论里**出现的条款号。

    踩过的坑：一开始把召回条款的原文一起拿去做校验，结果条款正文里大量
    "依照本法第X条"的交叉引用被判成"编造"（假阳性）。生成的结论才需要校验，
    引用原文本身就是原文，不需要证明自己存在。

    两种编号都要认：法条是"第X条"，平台规则是"2.3.1"。
    """
    cited = set(CLAUSE_NO_RE.findall(answer)) | set(PLATFORM_NO_RE.findall(answer))
    allowed = {c["clause_no"] for c in clauses}
    # 层级编号互认：模型写「2.3」而召回的是「2.3.1」是同一族，不是编造。
    # 不做这一步，两段式编号一放进来就会大量假阳性，逼系统每答一次都重试。
    fake = sorted(n for n in cited if not any(_same_family(n, a) for a in allowed))
    return (not fake), fake


def compose(query: str, clauses: list[dict], risk_level: str, reason: str,
            order: dict | None, scene: str | None, low_conf: bool = False) -> dict:
    if not clauses:
        return {
            "conclusion": "现行法规与平台规则中未检索到可直接依据的条款，建议转人工核实。",
            "notice": "本系统只回答有条款依据的问题，不做推测。",
            "handoff": True, "timeouts": [], "fake_citations": [],
            "citation_verified": True,
        }

    # 低置信：有召回但相关度不够 —— 宁可说"没有直接依据"，也不要拿不相干的条款硬答
    if low_conf:
        top = clauses[0]
        return {
            "conclusion": (f"现行法规中未检索到对「{query.strip('？?')}」的直接规定。"
                           f"最接近的条款是《{top['doc_name']}》{top['clause_no']}，但它规范的是另一类情形，"
                           f"不能据此回答你的问题。\n\n这类时限/流程通常由平台规则约定，"
                           f"平台规则层尚未录入，因此建议转人工核实。"),
            "notice": "低置信不硬答：召回分数低于阈值时主动声明没有依据，而不是拿相近条款凑答案。",
            "handoff": True,
            "handoff_reason": "法规层无直接依据，需人工按平台规则核实",
            "timeouts": [],
            "citation_verified": True,
            "fake_citations": [],
            "order": order,
            "scene": scene,
            "risk_level": risk_level,
        }

    top = clauses[0]
    src = f"《{top['doc_name']}》{top['clause_no']}"
    body = top["content"]

    if risk_level == "L3":
        conclusion = (f"该问题涉及处罚金额或法律定性，按设计**不由 AI 给出结论**。"
                      f"可直接依据的条款是{src}，原文如下，最终认定以平台/监管部门为准。")
    elif risk_level == "L2":
        conclusion = f"依据{src}，相关规定如下；时限类要求请以条款原文与平台最新公告为准。"
    else:
        conclusion = f"依据{src}，规定如下。"

    # 结论里补一句最相关的原文摘要（首句，去掉"第X条"前缀）
    first = re.split(r"[。；]", body.strip())[0]
    if len(first) > 12:
        conclusion += f"\n\n核心要求：{first}。"

    notice = "以上依据现行有效条款；如遇规则更新，以最新公布版本为准。"
    if risk_level == "L2":
        notice = "时限条款存在版本差异风险，请同时核对平台最新公告；本回答不作为申诉唯一依据。"
    if risk_level == "L3":
        notice = "高风险事项已生成人工工单，AI 不代为判断处罚结果。"

    answer_text = conclusion          # 只校验生成部分，原文不参与校验
    ok, fake = verify_citations(answer_text, clauses)

    # 时限要写进结论。
    # 踩过的坑：条款正文里明明有"36 小时内"，但结论只摘了首句，
    # 用户问"多久处理"，答复里却找不到那个时限 —— 答案对不对先不说，
    # 该给的信息没给。时限类问题必须把时限摆在结论里，不能让人去正文里捞。
    # L3 也一样给：L3 的意思是"处罚结果不由 AI 判定"，不是"什么都不说"。
    # 时限是客观事实，扣着不给只会让用户连该做的时间点都不知道。
    tos = extract_timeouts(body)
    if tos:
        conclusion += "\n\n本条时限：" + "、".join(tos) + "。"

    return {
        "conclusion": conclusion,
        "notice": notice,
        "handoff": risk_level == "L3",
        "handoff_reason": reason,
        "timeouts": extract_timeouts(body),
        "citation_verified": ok,
        "fake_citations": fake,
        "order": order,
        "scene": scene,
        "risk_level": risk_level,
        "generator": "template",
    }


def compose_llm(query: str, clauses: list[dict], risk_level: str, reason: str,
                order: dict | None, scene: str | None, low_conf: bool = False,
                max_retry: int = 1, history: list[dict] | None = None) -> dict:
    """大模型生成 + 引用校验闸门。

    闸门逻辑（这是本项目的核心差异点，不是"接个模型就完了"）：
      模型输出 → 抽出所有条款号 → 与召回集比对
        ├─ 全部命中      → 采纳，标注 generator=llm:<model>
        └─ 存在编造编号  → 带着"你编造了第X条"重试一次
                            └─ 仍编造 → 整条丢弃，退回模板答案

    为什么不能"事后删掉编造的引用"：
      删掉条款号，结论本身仍然建立在虚构条款上，用户看到的是没有出处的断言，
      比引用错了更危险（引用错了至少能被审计发现）。
    """
    client = llm_mod.get_llm()
    meta: dict = {"enabled": client.enabled, "available": client.available(),
                  "provider": client.provider, "model": client.model}

    # 没配、关了、或根本没有条款可依据 → 直接模板，不浪费一次调用
    if not client.available() or not clauses or low_conf:
        out = compose(query, clauses, risk_level, reason, order, scene, low_conf=low_conf)
        out["generator"] = "template"
        out["llm"] = {**meta, "skip": "not_configured" if not client.available()
                      else ("no_clauses" if not clauses else "low_confidence")}
        return out

    messages = llm_mod.build_messages(query, clauses, risk_level, scene, history=history)
    text, call_meta = client.chat(messages)

    if not text:
        out = compose(query, clauses, risk_level, reason, order, scene, low_conf=low_conf)
        out["generator"] = "template"
        out["llm"] = {**meta, "fallback": "call_failed", "error": call_meta.get("error", "")[:160]}
        return out

    parsed = llm_mod.parse_output(text) or {}
    conclusion = str(parsed.get("conclusion") or "").strip()
    notice = str(parsed.get("notice") or "").strip()
    ok, fake = verify_citations(conclusion, clauses)

    # 引用校验不通过：重试一次，明确告知模型哪些编号是它自己编的
    if not ok and max_retry > 0:
        fix = (f"你上一次的回答引用了不存在的条款编号：{', '.join(fake)}。\n"
               f"本次只允许使用这些编号：{', '.join(c['clause_no'] for c in clauses)}。\n"
               f"请重写，仍然只输出 JSON。")
        text2, _ = client.chat(messages + [{"role": "assistant", "content": text},
                                           {"role": "user", "content": fix}],
                               temperature=0.0)
        parsed2 = llm_mod.parse_output(text2) if text2 else None
        if parsed2 and str(parsed2.get("conclusion") or "").strip():
            c2 = str(parsed2["conclusion"]).strip()
            ok2, fake2 = verify_citations(c2, clauses)
            if ok2:
                conclusion, notice = c2, str(parsed2.get("notice") or notice).strip()
                ok, fake = ok2, fake2

    if not ok or not conclusion:
        out = compose(query, clauses, risk_level, reason, order, scene, low_conf=low_conf)
        out["generator"] = "template"
        out["llm"] = {**meta, "fallback": "citation_check_failed",
                      "fake_citations": fake, "ms": call_meta.get("ms")}
        return out

    top = clauses[0]
    if risk_level == "L3":
        notice = notice or "高风险事项已生成人工工单，AI 不代为判断处罚结果。"
    elif not notice:
        notice = "以上依据现行有效条款；如遇规则更新，以最新公布版本为准。"

    return {
        "conclusion": conclusion,
        "notice": notice,
        # handoff：模型判断 + 规则双重保险，任一为真就转人工，不允许"模型说不用转"就放过
        "handoff": bool(parsed.get("handoff")) or risk_level == "L3",
        "handoff_reason": reason,
        "timeouts": extract_timeouts(top["content"]),
        "citation_verified": True,
        "fake_citations": [],
        "order": order,
        "scene": scene,
        "risk_level": risk_level,
        "generator": f"llm:{client.model}",
        "llm": {**meta, "ms": call_meta.get("ms"), "tokens": call_meta.get("tokens")},
    }
