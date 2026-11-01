"""智能客服：四级漏斗路由（Redis → FAQ BM25 → 大模型 → 人工兜底）。

为什么是漏斗而不是直接上大模型：
  大模型回答一条"什么时候发货"要 1~3 秒、按 token 计费、还有 3% 的概率编造。
  而这句问题店里一个月被问 4000 次，答案一个字都不该变。
  所以顺序固定为「越便宜越靠前、越确定越靠前」：

    ① Redis 精确缓存     ~1ms   之前有人问过一模一样的问题（归一化后）
    ② MySQL FAQ + BM25   ~5ms   ≥0.88 直接出标准答案，措辞统一、可审计、零成本
    ③ 大模型             ~1.5s  知识库没有，但能基于知识库和店铺政策组织语言
    ④ 人工兜底           —      模型也没把握 / 涉及赔付 / 买家情绪激动

  这条链路的每一层都有明确的"命中条件"和"放弃条件"，不做"模型觉得可以就回答"。
  核心指标是**机器人独立解决率**，而不是"AI 参与率"——把没把握的问题硬答出去，
  换来的是差评和投诉，比转人工贵得多。

三个必须守住的点：
  * 缓存只存知识库直出的答案。大模型生成的回复可能带上下文（订单号、买家名字），
    一旦按问题文本缓存，A 买家的问题会把 B 买家的订单信息回给 C。
  * 转人工必须有工单落库。只在对话框里打一句"已转人工"而不建工单，
    就是把人晾在流程外面——没人知道该处理谁。
  * 坐席接管后机器人必须闭嘴。机器人和人同时回复是客服系统最招骂的故障。
"""
from __future__ import annotations

import hashlib
import re
import time

import common
import service.cache as cache
import service.faq as faq
import service.gov as gov
from api import schema
from generation.llm import get_llm

CACHE_TTL = 1800          # 知识库直出答案缓存 30 分钟
RATE_LIMIT = 40           # 单个会话每分钟提问上限

# ---------------------------------------------------------------- 意图

INTENT_RULES: list[tuple[str, re.Pattern]] = [
    ("投诉纠纷", re.compile(r"投诉|举报|起诉|律师|12315|曝光|差评|骗子|欺诈|假货|维权|消协")),
    ("退款退货", re.compile(r"退款|退钱|退货|退回|仅退款|退款失败|到账")),
    ("物流发货", re.compile(r"发货|快递|物流|到货|签收|单号|寄|配送|几天能到|揽收")),
    ("换货售后", re.compile(r"换货|换一个|保修|质保|维修|坏了|破损|瑕疵|少发|漏发|发错")),
    ("发票", re.compile(r"发票|开票|税号|专票|普票|报销")),
    ("优惠价格", re.compile(r"优惠|便宜|折扣|券|价保|降价|活动|满减|积分|会员")),
    ("商品咨询", re.compile(r"尺码|码数|材质|面料|颜色|色差|现货|库存|补货|定制|正品|真假")),
    ("支付问题", re.compile(r"支付|付款|分期|花呗|重复付|付不了")),
    ("订单账户", re.compile(r"订单|合并|取消|改地址|改信息|密码|登录")),
]

# 强制转人工：不经过检索，也不让模型去"努力回答"。
# 这几类问题答错一次的代价（投诉、罚款、舆情）远高于转人工的人力成本。
FORCE_HANDOFF: list[tuple[str, re.Pattern, str]] = [
    ("user_request", re.compile(r"转人工|人工客服|要人工|找人工|真人|转接|客服在吗|叫你们客服"), "买家主动要求人工"),
    ("complaint", re.compile(r"投诉|举报|起诉|律师|12315|曝光|消协|维权|法院|报警"), "投诉/维权类，禁止机器人答复"),
    ("fraud", re.compile(r"假货|售假|欺诈|骗人|骗子|诈骗|三无|以次充好"), "涉假涉诈，禁止机器人答复"),
    ("compensation", re.compile(r"赔偿|赔付|精神损失|误工费|三倍|十倍"), "涉及赔付金额，需人工核定"),
    ("abuse", re.compile(r"垃圾店|黑店|滚|傻|废物|去死"), "买家情绪激烈"),
]


# 商家问规则时，"要人工"的含义完全不同：不是转店铺客服，而是转规则运营。
# 另一类必须拦住的是"查我自己的账号数据"——助手没有权限也没有数据，
# 让它猜等于让它编一个扣分数字出来。
FORCE_HANDOFF_RULE: list[tuple[str, re.Pattern, str]] = [
    ("case_lookup", re.compile(r"(我|我们)(的|家)?(店|店铺|链接|商品|账号|保证金|货款)"
                               r".{0,8}(被|扣|封|下架|限权|处罚|冻结)|"
                               r"扣了我|我被扣|为什么扣我|我的扣分"), "查询本店个案记录，助手无账号数据"),
    ("user_request", re.compile(r"转人工|人工客服|找人工|真人|联系平台|找运营"), "商家主动要求人工"),
    ("legal", re.compile(r"起诉|律师|法院|仲裁委|市场监管|12315|工商|打官司"), "涉诉讼/监管投诉，转合规专员"),
]

# 运营问治理时，唯一必须拦住的是"给在办案子下结论"。
# 助手可以回答口径，不能替代仲裁员——否则一次口头结论就会被截图当依据。
FORCE_HANDOFF_GOV: list[tuple[str, re.Pattern, str]] = [
    ("case_judge", re.compile(r"(这个|这单|该|帮我|给我).{0,6}(案子|纠纷|投诉|单).{0,8}"
                              r"(怎么|如何|该不该)?(判|裁|处理)|判一下|直接判|怎么裁"),
     "个案裁决不由助手给结论，请走仲裁台"),
    ("user_request", re.compile(r"转人工|找仲裁|叫仲裁员|联系合规"), "运营主动要求人工"),
]


def intent_of(text: str) -> str:
    for name, pat in INTENT_RULES:
        if pat.search(text):
            return name
    return "其他"


def force_handoff(text: str, scope: str = "shop") -> tuple[bool, str, str]:
    """按板块选规则集。店铺客服那套（投诉/涉假/赔付/辱骂）套到商家身上
    会把"售假扣多少分"直接拦成转人工——那是规则助手最该答的问题。"""
    rules = (FORCE_HANDOFF if scope == "shop"
             else FORCE_HANDOFF_RULE if scope == "rule" else FORCE_HANDOFF_GOV)
    for reason, pat, label in rules:
        if pat.search(text):
            return True, reason, label
    return False, "", ""


# ---------------------------------------------------------------- 板块
#
# 三个角色进的是三个不同的咨询口，不是同一个口换套文案：
#
#   buyer     → 店铺客服（scope=shop）      这家店怎么发货、怎么退、开不开发票
#   merchant  → 平台规则助手（scope=rule）  我这样会不会违规、扣几分、怎么申诉
#   ops       → 治理规范助手（scope=gov）   该引哪一条、法律怎么规定、能不能这么判
#
# 必须拆开的理由很具体：同一句"扣几分"，买家问的是"我要不要赔"，
# 商家问的是"我这单扣多少"，运营问的是"依据哪条、能不能撤"。一个知识库
# 答不了三个问题；硬答就是给错答案，而错答案比不答更容易招投诉。
# 板块配置里放的不只是标题，还有「这个窗口长什么样」。
#
# 以前这些文案写在前端 SKIN 里，服务端只管标题和快捷问题。结果两边各改各的：
# 治理板块的头部还挂着买家的「平台介入」按钮，运营点下去是替别人投诉；
# 发完一条消息，状态栏又被写回「机器人 · 秒回」，治理板块的"规范库 · 在线"没了。
# 一个窗口服务三个角色，外观和动作就必须和板块配置放在同一个地方，
# 否则迟早会有一处忘记改。
SCOPES: dict[str, dict] = {
    "shop": dict(
        key="shop", label="店铺客服", audience="buyer", who="买家",
        title="在线客服", desc="发货、退款、发票、尺码",
        avatar="客", intro="以下为买家视角，消息会实时同步到客服工作台",
        greet="你好，我是店铺智能客服，有什么可以帮您？",
        trace="每句话都会走这条漏斗，命中即止",
        placeholder="说吧，客服在听…",
        states={"bot": "机器人 · 秒回", "human": "人工客服 · 已接入",
                "escalated": "已转平台介入 · 平台处理中"}),
    "rule": dict(
        key="rule", label="平台规则助手", audience="merchant", who="商家",
        title="平台规则助手", desc="违规判定、扣分标准、申诉流程",
        avatar="规", intro="依据《商家违规行为管理规则》回答，条款号与扣分以规则原文为准",
        greet="我是平台规则助手。可以问违规怎么判定、扣多少分、怎么申诉。",
        trace="本板块只在平台规则库里检索，不跨域",
        placeholder="问违规判定、扣分、申诉…",
        states={"bot": "规则库 · 在线", "human": "规则运营 · 已接入",
                "escalated": "已转规则运营"}),
    "gov": dict(
        key="gov", label="治理规范助手", audience="ops", who="运营",
        title="治理规范助手", desc="商家规范、法律条文、裁决口径",
        avatar="治", intro="依据商家规范与法律法规回答，只给依据和口径，不对在办案件下结论",
        greet="我是治理规范助手。可以问条款依据、法律条文和处置口径，也可以挑一单看它适用哪几条。",
        trace="本板块只在治理规范库里检索，不跨域",
        placeholder="问条款依据、法律条文…",
        states={"bot": "规范库 · 在线", "human": "合规专员 · 已接入",
                "escalated": "已转人工复核"}),
}


def scope_of(sess: dict | None) -> str:
    s = (sess or {}).get("scope") or "shop"
    return s if s in SCOPES else "shop"


def audience_of(scope: str) -> str:
    return SCOPES.get(scope, SCOPES["shop"])["audience"]


# ---------------------------------------------------------------- 大模型

CS_SYSTEM = {
    "shop": (
        "你是电商店铺「{shop}」的在线客服机器人，说话对象是来咨询的买家。\n"
        "硬性规则：\n"
        "1. 只能依据【知识库】和【店铺政策】回答，不许编造物流单号、退款金额、到货日期、补偿方案。\n"
        "2. 不许承诺具体到达时间，只能说\"预计\"；不许承诺赔偿金额；不许评价平台或其他商家。\n"
        "3. 知识库里找不到依据时，不要猜，把 handoff 设为 true 交给人工。\n"
        "4. 涉及质量投诉、假货、赔付、法律问题、情绪激动的买家，handoff 一律为 true。\n"
        "5. 语气自然友好，2~3 句话，不超过 80 字，不要用 markdown、不要用列表。\n"
        "输出严格 JSON，不要代码块，不要多余字符：\n"
        '{{"reply":"回复买家的话","handoff":false,"intent":"物流发货","reason":""}}'
    ),
    # 商家问的是"规则怎么算"，答案必须带条款号和分值，且不许查个案。
    # 商家最怕的是"助手说不用扣，结果扣了 24 分"——所以个案一律不猜。
    "rule": (
        "你是电商平台的「平台规则助手」，说话对象是平台内商家。\n"
        "硬性规则：\n"
        "1. 只能依据【规则库】和【规则摘要】回答。引用必须带条款号，扣分必须与条款原文一致，不许估算或四舍五入。\n"
        "2. 不查账号数据：店铺累计多少分、某张处罚单为什么生效，这类问题不要猜，handoff 设为 true。\n"
        "3. 不能给出\"这样做没问题\"这类免责结论；不确定就说不确定。\n"
        "4. 涉及诉讼、监管部门投诉的，指路平台合规专员，不要提供法律意见。\n"
        "5. 语气专业克制，2~4 句话，不超过 120 字，不要用 markdown、不要用列表。\n"
        "输出严格 JSON，不要代码块：\n"
        '{{"reply":"回复商家的话","handoff":false,"intent":"处罚与扣分","reason":""}}'
    ),
    # 运营要的是"可引用的依据"，而不是"大概是这样"。
    "gov": (
        "你是电商平台的「治理规范助手」，服务对象是平台运营与仲裁员。\n"
        "硬性规则：\n"
        "1. 回答必须给出出处：平台规则条款号，或法律条文（法律名 + 条号）。没有出处就说没有依据。\n"
        "2. 不得对在办的具体案件给出裁决结论，只提供判断口径与依据；涉及个案一律 handoff。\n"
        "3. 严格区分\"规则明文规定\"与\"行业惯例/我们过去的做法\"，不许把惯例说成规则。\n"
        "4. 涉及人身安全、售假、欺诈的，提示必须人工裁决并说明理由。\n"
        "5. 语气像合规同事，3~5 句话，不超过 160 字，不要用 markdown、不要用列表。\n"
        "输出严格 JSON，不要代码块：\n"
        '{{"reply":"回复运营的话","handoff":false,"intent":"处罚依据","reason":""}}'
    ),
}


def _rule_digest() -> str:
    """从裁决引擎的条款表生成规则摘要。

    刻意不手写：FAQ、模型提示词、裁决引擎如果各存一份扣分表，
    迟早出现"助手说扣 2 分、系统扣了 24 分"。三处共用 gov.DTYPES 一个源，
    改一处三处同时变——这是"助手说的话和系统做的事必须一致"的技术保证。
    """
    lines = []
    for d in gov.DTYPES.values():
        if not d.get("clause"):
            continue
        lines.append(f"{d['clause']} {d['label']}：每次 {d['points']:g} 分（{d['doc']}）")
    for pts, _t, label, why in gov.NODES:
        lines.append(f"累计满 {pts} 分 → {label}")
    return "\n".join(lines)

# 这些说法出现，说明模型开始自己加戏（承诺时效/承诺赔付），必须拦下来。
RISKY_PAT = re.compile(r"保证.{0,6}(到|送达|退款|赔)|一定.{0,4}(赔|退)|"
                       r"百分之百|100%|全额赔偿|赔付.{0,4}元|明天.{0,2}到")


def _ctx(cands: list[dict], limit: int = 3) -> str:
    parts = []
    for i, c in enumerate(cands[:limit], 1):
        parts.append(f"[{i}] 问：{c['question']}\n答：{c['answer'][:180]}")
    return "\n".join(parts) if parts else "（知识库无相关内容）"


def llm_reply(text: str, cands: list[dict], shop: str = "本店",
              scope: str = "shop",
              clauses: list[dict] | None = None) -> tuple[str | None, bool, dict]:
    """返回 (回复, 是否要求转人工, 元信息)。任何异常都不抛，返回 None 交给上层兜底。"""
    llm = get_llm()
    if not llm.available():
        return None, True, {"error": "not_configured"}

    # 上下文也按板块给。给商家的模型塞"7 天无理由、满 99 包邮"这种店铺政策，
    # 它就会拿店铺话术回答规则问题——错得很自然，很难发现。
    if scope == "shop":
        extra = ("【店铺政策】发货 48 小时内出库；满 99 包邮；7 天无理由；15 天价保；"
                 "在线客服 9:00-22:00。")
        ask = "【买家问题】"
    elif scope == "rule":
        extra = (f"【平台规则摘要】（与裁决引擎同源，数值不得改动）\n{_rule_digest()}\n"
                 f"【申诉口径】处罚单生效后 7 天内可申诉，AI 初审 + 人工终审；"
                 f"申诉成立撤销处罚并连带撤销节点处罚。")
        ask = "【商家问题】"
    else:
        extra = (f"【平台规则摘要】（与裁决引擎同源，数值不得改动）\n{_rule_digest()}\n"
                 f"【法律索引】电子商务法 17/20/27/31/35/38/49/61/63 条；"
                 f"消费者权益保护法 24/25/44/55 条。引用时写法律名 + 条号。")
        ask = "【运营问题】"

    # 规则/治理板块把 RAG 召回的条款原文也塞进去：
    # 只给"规则摘要"时模型只能背扣分数值，给不了条号与依据；
    # 商家真正要的是"哪一条、怎么写的"，所以必须回到 406 条条款本体。
    clause_block = ""
    if scope in ("rule", "gov") and clauses:
        lines = []
        for i, c in enumerate(clauses[:3], 1):
            lines.append(f"[{i}] 《{c.get('doc_name')}》{c.get('clause_no')}"
                         f"\n{str(c.get('content') or '')[:400]}")
        clause_block = "【条款原文】\n" + "\n\n".join(lines) + "\n\n"

    user = f"【知识库】\n{_ctx(cands)}\n\n{clause_block}{extra}\n\n{ask}\n{text}\n\n请按 JSON 输出。"
    msgs = [{"role": "system", "content": CS_SYSTEM.get(scope, CS_SYSTEM["shop"]).format(shop=shop)},
            {"role": "user", "content": user}]
    out, meta = llm.chat(msgs, temperature=0.3, max_tokens=300)
    if not out:
        return None, True, meta

    from generation.llm import parse_output
    d = parse_output(out) or {}
    reply = (d.get("reply") or d.get("conclusion") or "").strip()
    if not reply:
        return None, True, {**meta, "error": "empty_reply"}

    handoff = bool(d.get("handoff"))
    # 输出侧安全闸门：模型承诺了时效或赔付，即使它自己说不用转人工，也要转。
    # 这类话术一旦发出去就是承诺，后面不兑现就是投诉。
    if RISKY_PAT.search(reply):
        handoff = True
        meta = {**meta, "risk_filter": "blocked_promise"}
    meta = {**meta, "intent": d.get("intent") or "", "reason": d.get("reason") or ""}
    return reply[:400], handoff, meta


# ---------------------------------------------------------------- 兜底话术

def _no_llm_reply(cands: list[dict], scope: str = "shop") -> str:
    tip = ""
    if cands:
        tip = f"你也可以先看看这条：「{cands[0]['question']}」"
    if scope == "shop":
        return ("抱歉，这个问题我暂时没有把握准确回答，已经帮您转接人工客服，"
                "客服看到消息会第一时间回复您。" + tip)
    if scope == "rule":
        return ("这个问题的规则库里还没有明确条款，已经转给平台规则运营确认，"
                "确认后会补进知识库。涉及处罚单的，可以直接在「纠纷中心」提交申诉。" + tip)
    return ("知识库里没有能直接引用的条款，已提交规则补充。"
            "涉及在办案件的，请走仲裁台按现有证据处理，助手不对个案下结论。" + tip)


# ---------------------------------------------------------------- 会话

def new_session(shop_id: int | None, buyer: str = "买家", channel: str = "淘宝",
                tag: str = "", buyer_id: int | None = None,
                scope: str = "shop") -> dict:
    """开一个会话。

    scope 决定这个会话进哪个板块：
      shop 买家问店铺，必须挂 shop_id，且受店铺经营状态约束
      rule 商家问平台规则，不挂店铺——挂上就会出现在该店的坐席队列里，
           客服会看到"商家自己来咨询规则"的会话，纯噪声
      gov  运营问治理口径，同样不挂店铺
    """
    scope = scope if scope in SCOPES else "shop"
    if scope == "shop":
        # 平台监管的处罚要能真的掐断经营。只写一条"店铺已停业"的处罚单，
        # 而客服照样接待、订单照样能下，那处罚就只是后台里的一个数字。
        st = gov.shop_status(shop_id)
        if st == "closed":
            raise ValueError("该店铺已被平台清退，无法发起咨询")
        if st == "suspended":
            raise ValueError("该店铺已被平台暂停营业，整改完成后恢复接待")
    else:
        shop_id = None
    sid = schema.new_sid()
    ts = schema.now()
    common.execute(
        """INSERT INTO t_cs_session (id, shop_id, buyer_id, buyer_name, buyer_tag,
               channel, scope, status, last_msg, msg_count, handoff, created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'bot','',0,0,%s,%s)""",
        (sid, shop_id, buyer_id, (buyer or "提问人")[:60], (tag or "")[:30],
         channel or "站内", scope, ts, ts))
    return {"id": sid, "scope": scope}


def get_session(sid: str) -> dict | None:
    rows = common.fetch_all("SELECT * FROM t_cs_session WHERE id=%s", (sid,))
    return rows[0] if rows else None


def _push(sid: str, role: str, content: str, source: str = "", score: float = 0,
          faq_id: int | None = None, intent: str = "", latency_ms: int = 0,
          sender_role: str = "") -> int:
    # 谁发的：买家 / 机器人 / 商家客服 / 平台运营 / 管理员。
    # role 只能表达"买家/机器人/人工"这种粗粒度，三方同处一个会话时
    # 靠 sender_role 才分得清"人工客服"到底是商家还是平台运营。
    if not sender_role and role in ("buyer", "bot", "system"):
        sender_role = role
    mid = common.insert(
        """INSERT INTO t_cs_message (session_id, role, content, source, score, faq_id,
               intent, latency_ms, sender_role, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (sid, role, content, source, score, faq_id, intent, latency_ms, sender_role, schema.now()))
    common.execute(
        "UPDATE t_cs_session SET msg_count=msg_count+1, last_msg=%s, updated_at=%s WHERE id=%s",
        (content[:250], schema.now(), sid))
    return mid


def _ticket(sid: str, reason: str, question: str, bot_reply: str, priority: int = 0) -> int:
    """建工单。同一会话已有待处理工单就不再重复建——
    买家连发三条没人理，会生成三个工单，坐席要关三次，这是纯负担。"""
    rows = common.fetch_all(
        "SELECT id FROM t_ticket WHERE session_id=%s AND status IN ('pending','processing') "
        "ORDER BY id DESC LIMIT 1", (sid,))
    if rows:
        return int(rows[0]["id"])
    tid = common.insert(
        """INSERT INTO t_ticket (session_id, reason, question, bot_reply, status,
               priority, created_at) VALUES (%s,%s,%s,%s,'pending',%s,%s)""",
        (sid, reason, question[:1000], bot_reply[:1000], priority, schema.now()))
    common.execute("UPDATE t_cs_session SET handoff=1, updated_at=%s WHERE id=%s",
                   (schema.now(), sid))
    return tid


# ---------------------------------------------------------------- 主链路

def _handoff_reply(reason: str, scope: str) -> str:
    """强制转人工的话术。三个板块的"人工"不是同一个人：
    店铺客服 = 店家客服，规则助手 = 平台规则运营，治理助手 = 合规/仲裁。"""
    if scope == "shop":
        return ("已经帮您转接人工客服，请稍等，客服会马上跟进处理您的问题。"
                if reason == "user_request" else
                "这个问题需要人工客服为您核实处理，已经帮您转接，请稍等。")
    if scope == "rule":
        if reason == "case_lookup":
            return ("助手看不到你店铺的账号数据，没法回答「我这单扣了多少」。"
                    "已转给平台规则运营；处罚明细可以在「纠纷中心」的处罚记录里查，"
                    "对处罚不服可在生效后 7 天内提交申诉。")
        if reason == "legal":
            return "涉及诉讼或监管投诉的问题，已转平台合规专员跟进，助手不提供法律意见。"
        return "已转平台规则运营，会在 1 个工作日内回复你。"
    if reason == "case_judge":
        return ("助手只提供条款口径，不对在办案件下结论。请在「平台监管」仲裁台按现有证据裁决，"
                "涉假、欺诈、人身安全以及诉求超过 1000 元的必须人工裁决。")
    return "已转合规复核，稍后回复。"


def handle(sid: str, text: str, sender_role: str = "buyer") -> dict:
    """处理一条提问，返回路由结果。落库、建工单都在这里，视图层只管鉴权。"""
    t0 = time.time()
    q = (text or "").strip()[:500]
    if not q:
        raise ValueError("消息不能为空")

    sess = get_session(sid)
    if not sess:
        raise ValueError("会话不存在")

    scope = scope_of(sess)
    aud = audience_of(scope)

    # 下面两个分支只属于店铺客服：它们回答的是"这家店还能不能接待"。
    # 商家问规则、运营问治理都不挂店铺，硬套会得到"该店铺已被清退"这种莫名其妙的答复。
    if scope == "shop":
        # 已转平台介入：会话入口要收口。否则同一个诉求在客服和纠纷两个入口各说一半，
        # 仲裁员看到的是两份残缺记录，还不如让人在一个地方说完。
        if sess.get("status") == "escalated":
            ii = intent_of(q)
            d = dispute_of(sid)
            tip = (f"本次会话已转入平台纠纷流程（{d['dispute_no']}），"
                   f"请到「纠纷中心」继续补充材料，平台的处理进展也会同步在那里。"
                   if d else
                   "本次会话已转入平台纠纷流程，请到「纠纷中心」继续沟通。")
            _push(sid, "buyer", q, intent=ii, sender_role=sender_role)
            _push(sid, "bot", tip, source="rule", intent=ii)
            return {"reply": tip, "source": "rule", "handoff": True,
                    "ticket_id": None, "score": 0, "faq_id": None, "intent": ii,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "trace": {"layers": [{"layer": "escalated", "hit": True,
                                          "dispute_no": (d or {}).get("dispute_no", "")}]}}

        # 会话建立后店铺被停业：不能再让机器人按话术接待。
        # 这里不抛错而是落一条说明 + 工单，因为买家的消息已经发出去了，
        # 报错只会让他以为系统坏了。
        st = gov.shop_status(sess.get("shop_id"))
        if st in ("suspended", "closed"):
            ii = intent_of(q)
            tip = ("该店铺已被平台暂停营业，客服暂停接待。您的诉求已记录，"
                   "请在「纠纷中心」向平台申请介入，平台将直接处理。"
                   if st == "suspended" else
                   "该店铺已被平台清退。您的诉求已记录，请在「纠纷中心」申请平台介入。")
            _push(sid, "buyer", q, intent=ii, sender_role=sender_role)
            _push(sid, "bot", tip, source="rule", intent=ii)
            tid = _ticket(sid, "shop_suspended", q, tip, priority=8)
            return {"reply": tip, "source": "rule", "handoff": True, "ticket_id": tid,
                    "score": 0, "faq_id": None, "intent": ii,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "trace": {"layers": [{"layer": "shop_suspended", "hit": True,
                                          "status": st}]}}

    ok, _ = cache.rate_ok(f"cs:{sid}", RATE_LIMIT)
    if not ok:
        raise RuntimeError(f"发送过于频繁，每分钟最多 {RATE_LIMIT} 条")

    intent = intent_of(q)
    # role='buyer' 在这里表示"提问方"：店铺板块是买家，rule 板块是商家，gov 板块是运营。
    # 保留字段值是为了不动历史数据与前端气泡逻辑，语义差异由 scope 承载。
    _push(sid, "buyer", q, intent=intent, sender_role=sender_role)

    trace: dict = {"layers": [], "scope": scope, "audience": aud}

    # ④ 坐席已接管：机器人不插嘴，消息只落库等人回。
    if sess["status"] == "agent":
        trace["layers"].append({"layer": "human_takeover", "hit": True})
        return {"reply": "", "source": "human", "handoff": True, "ticket_id": None,
                "score": 0, "faq_id": None, "intent": intent,
                "latency_ms": int((time.time() - t0) * 1000), "trace": trace}

    # ➁ 强制转人工规则（在检索之前）：投诉、涉假、要人工，不该让模型去"努力回答"
    blocked, reason, label = force_handoff(q, scope)
    if blocked:
        reply = _handoff_reply(reason, scope)
        _push(sid, "bot", reply, source="rule", intent=intent,
              latency_ms=int((time.time() - t0) * 1000))
        pri = 9 if reason in ("complaint", "fraud", "legal") else \
              7 if reason == "user_request" else 6
        tid = _ticket(sid, reason, q, label, priority=pri)
        trace["layers"].append({"layer": "force_handoff", "hit": True, "reason": reason})
        return {"reply": reply, "source": "rule", "handoff": True, "ticket_id": tid,
                "score": 0, "faq_id": None, "intent": intent,
                "latency_ms": int((time.time() - t0) * 1000), "trace": trace}

    nq = faq.norm(q)
    ver = faq.version()
    # 店铺视野：只有店铺板块才按店隔离。规则/治理板块是平台统一口径，
    # 硬塞一个 shop_id 进去会让"平台规则"跟着某家店的私货走。
    shop_key = int(sess["shop_id"]) if (scope == "shop" and sess.get("shop_id")) else None

    # ① Redis 精确缓存（归一化后同一句话）
    # key 必须带知识域：同一句"扣几分"在三个域有三个正确答案，共用一个 key
    # 就会出现"买家看到商家话术"这种事，而且很难复现。
    # 同样必须带店铺：A 店"什么时候发货"答 48 小时，B 店答 7 天预购，
    # 共用一个 key 的结果是 B 店买家看到 A 店的承诺，然后拿这段话去投诉 B 店。
    ck = f"rr:cs:v{ver}:a{aud}:s{shop_key or 0}:q:{hashlib.md5(nq.encode()).hexdigest()}"
    hit = cache.get(ck)
    if hit:
        lat = int((time.time() - t0) * 1000)      # 报本次真实耗时，不是上次的
        _push(sid, "bot", hit["reply"], source="redis", score=hit.get("score") or 0,
              faq_id=hit.get("faq_id"), intent=intent, latency_ms=lat)
        trace["layers"].append({"layer": "redis", "hit": True, "key": ck,
                                "origin": hit.get("origin")})
        return {"reply": hit["reply"], "source": "redis", "origin": hit.get("origin"),
                "handoff": False, "ticket_id": None, "score": hit.get("score") or 0,
                "faq_id": hit.get("faq_id"), "intent": intent,
                "latency_ms": lat, "trace": trace}
    trace["layers"].append({"layer": "redis", "hit": False, "key": ck})

    # ➁ MySQL FAQ + 归一化 BM25（只在本域、本店视野里检索）
    idx = faq.index(aud, shop_key)
    best, cands = idx.best(q)
    trace["layers"].append({
        "layer": "faq_bm25", "hit": best is not None, "audience": aud,
        "shop_id": shop_key, "threshold": faq.THRESHOLD,
        "top": [{"id": c["id"], "q": c["question"], "sim": c["sim"],
                 "cov": c["cov"], "sat": c["sat"]} for c in cands[:3]],
    })

    # ➁.5 RAG 条款检索（规则/治理板块）。
    # MySQL FAQ 只是"高频问答对"，覆盖不了 406 条条款里的专业判断；
    # 商家问的绝大多数是"哪一条法规怎么写的"，答案必须回到条款本体。
    # 这里复用 qa 的混合检索器 = BM25 ∥ 向量(Milvus) → RRF → 精排，与 /api/ask 同一条链路。
    rag_hits: list[dict] = []
    if scope in ("rule", "gov"):
        try:
            from service import qa as _qa
            if getattr(_qa.RT, "index", None) is not None:
                rag_hits = [{**d, "score": s}
                            for d, s in _qa.RT.index.search(q, top_k=3, scene=None)]
        except Exception:
            rag_hits = []
        trace["layers"].append({
            "layer": "rag", "hit": bool(rag_hits),
            "top": [{"doc": c.get("doc_name"), "no": c.get("clause_no"),
                     "score": round(float(c.get("score") or 0), 4)}
                    for c in rag_hits[:3]],
        })

    if best:
        lat = int((time.time() - t0) * 1000)
        _push(sid, "bot", best["answer"], source="faq", score=best["sim"],
              faq_id=best["id"], intent=intent, latency_ms=lat)
        faq.hit(best["id"])
        cache.set(ck, {"reply": best["answer"], "score": best["sim"],
                       "faq_id": best["id"], "origin": "faq"}, ttl=CACHE_TTL)
        return {"reply": best["answer"], "source": "faq", "origin": "faq",
                "handoff": False, "ticket_id": None, "score": best["sim"],
                "faq_id": best["id"], "intent": intent, "latency_ms": lat,
                "candidates": [{"id": c["id"], "q": c["question"], "sim": c["sim"]}
                               for c in cands[:3]],
                "trace": trace}

    # ③ 大模型兜底（带上本域知识库候选做上下文）
    shop = "本店"
    try:
        if sess.get("shop_id"):
            r = common.fetch_all("SELECT name FROM t_shop WHERE id=%s", (sess["shop_id"],))
            shop = r[0]["name"] if r else shop
    except Exception:
        pass

    reply, handoff, meta = llm_reply(q, cands, shop, scope, rag_hits)
    trace["layers"].append({"layer": "llm", "hit": bool(reply), "handoff": handoff,
                            "meta": meta})

    if reply and not handoff:
        lat = int((time.time() - t0) * 1000)
        _push(sid, "bot", reply, source="llm", score=(cands[0]["sim"] if cands else 0),
              intent=intent, latency_ms=lat)
        # 大模型回复不进缓存：它可能带了上下文（订单、称呼），按问题文本复用会串号。
        return {"reply": reply, "source": "llm", "handoff": False, "ticket_id": None,
                "score": cands[0]["sim"] if cands else 0, "faq_id": None,
                "intent": intent, "latency_ms": lat, "trace": trace}

    # ④ 人工兜底
    lat = int((time.time() - t0) * 1000)
    if reply:
        # 模型给了话术但要求转人工：先安抚，再转
        text_out = reply
        reason = meta.get("reason") and "llm_uncertain" or "llm_handoff"
    else:
        text_out = _no_llm_reply(cands, scope)
        reason = "llm_unavailable" if meta.get("error") == "not_configured" else "low_conf"
    _push(sid, "bot", text_out, source="template", intent=intent, latency_ms=lat)
    tid = _ticket(sid, reason, q, text_out, priority=6 if reason == "low_conf" else 4)
    trace["layers"].append({"layer": "handoff", "hit": True, "reason": reason,
                            "ticket_id": tid})
    return {"reply": text_out, "source": "template", "handoff": True, "ticket_id": tid,
            "score": cands[0]["sim"] if cands else 0, "faq_id": None,
            "intent": intent, "latency_ms": lat,
            "candidates": [{"id": c["id"], "q": c["question"], "sim": c["sim"]}
                           for c in cands[:3]],
            "trace": trace}


# ---------------------------------------------------------------- 坐席侧

def claim(sid: str, agent: dict) -> dict:
    sess = get_session(sid)
    if not sess:
        raise ValueError("会话不存在")
    common.execute(
        "UPDATE t_cs_session SET status='agent', updated_at=%s WHERE id=%s",
        (schema.now(), sid))
    common.execute(
        "UPDATE t_ticket SET status='processing', agent_id=%s, claimed_at=%s "
        "WHERE session_id=%s AND status='pending'",
        (agent["id"], schema.now(), sid))
    _push(sid, "system", f"{agent['display_name']} 已接入会话", source="system",
          sender_role=agent.get("role", ""))
    return {"ok": True}


def release(sid: str, agent: dict) -> dict:
    """把会话交还给机器人，工单标完成。"""
    common.execute(
        "UPDATE t_cs_session SET status='bot', updated_at=%s WHERE id=%s",
        (schema.now(), sid))
    common.execute(
        "UPDATE t_ticket SET status='done', closed_at=%s "
        "WHERE session_id=%s AND status IN ('pending','processing')",
        (schema.now(), sid))
    _push(sid, "system", "人工客服已结束本次会话", source="system")
    return {"ok": True}


def agent_say(sid: str, agent: dict, text: str) -> dict:
    q = (text or "").strip()[:500]
    if not q:
        raise ValueError("回复内容不能为空")
    sess = get_session(sid)
    if not sess:
        raise ValueError("会话不存在")
    if sess["status"] != "agent":
        raise ValueError("请先接管会话再回复")
    mid = _push(sid, "agent", q, source="human", intent=intent_of(q),
                sender_role=agent.get("role", ""))
    common.execute("UPDATE t_ticket SET status='processing', agent_id=%s WHERE session_id=%s "
                   "AND status='pending'", (agent["id"], sid))
    return {"ok": True, "message_id": mid}


GREETING = {"在吗", "在么", "在不在", "你好", "您好", "hi", "hello", "有人吗", "在"}


def distill(sid: str, user: dict, question: str | None = None) -> dict:
    """把人工回复沉淀成知识库条目。

    这是整套系统里唯一能让人力投入产生复利的地方：
    坐席今天回了 20 条知识库里没有的问题，明天这 20 条就该由机器人来答。
    没有这一步，客服系统永远只有"消耗人工"，没有"减少人工"。

    配对规则：取最后一轮「买家问 → 人工答」，并且跳过"在吗"这种寒暄。
    不跳过的后果很实际：坐席先说"在的"，再说答案，沉淀进知识库的
    就会是「在吗 → 支持七天无理由退货」，从此机器人对任何"在吗"都回一段退货政策。
    """
    rows = common.fetch_all(
        "SELECT role, content FROM t_cs_message WHERE session_id=%s "
        "AND role IN ('buyer','agent') ORDER BY id ASC LIMIT 400", (sid,))

    last_agent = -1
    for i, r in enumerate(rows):
        if r["role"] == "agent":
            last_agent = i
    if last_agent < 0:
        raise ValueError("这个会话里还没有人工回复，没有可沉淀的内容")

    q, a = question, rows[last_agent]["content"]
    if not q:
        for i in range(last_agent - 1, -1, -1):
            if rows[i]["role"] != "buyer":
                continue
            cand = (rows[i]["content"] or "").strip()
            if len(cand) >= 5 and cand not in GREETING:
                q = cand
                break
    if not q:
        raise ValueError("没找到有效的问题（只找到寒暄语），请在会话里让买家先描述问题")

    ts = schema.now()
    # 沉淀到会话所属的知识域：商家问规则沉淀出来的是规则知识，
    # 写进 buyer 域会让买家听到"你已被扣 0.5 分"这种话。
    # 店铺板块沉淀出来的还必须挂在**这家店**名下：坐席在 A 店跟买家谈好的
    # 特殊口径（比如"你这单我帮你申请补发"），写成平台通用条目，
    # B 店的买家也会听到同样的承诺。
    sess = get_session(sid) or {}
    sc = scope_of(sess)
    aud = audience_of(sc)
    shop_id = int(sess["shop_id"]) if (sc == "shop" and sess.get("shop_id")) else None
    # 审核流：人工沉淀先 pending，由 ops/admin 过审后才 enabled 进检索。
    # 直接 enabled 的风险是坐席答错当场污染全店口径——机器人从此对同类问题
    # 都说这句错话，比没有知识更糟。pending 期间不进 rows_from_db 的索引，
    # 只有审核通过调用才会真正 reload。
    fid = common.insert(
        """INSERT INTO t_faq (question, answer, category, audience, shop_id, keywords,
               aliases, priority, status, hit_count, source, created_by, created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,'','',0,'pending',0,'human',%s,%s,%s)""",
        (q[:250], a, intent_of(q), aud, shop_id, user["id"], ts, ts))
    return {"ok": True, "faq_id": fid, "question": q[:120], "audience": aud,
            "shop_id": shop_id, "status": "pending", "version": faq.version()}


def pending_faqs(limit: int = 200) -> list[dict]:
    """待审队列。任何人工沉淀 / 主动写入但还没过审的条目都进这里。"""
    return common.fetch_all(
        """SELECT f.id, f.question, f.answer, f.category, f.audience, f.shop_id,
                  f.priority, f.status, f.source, f.created_at,
                  u.display_name AS creator_name
           FROM t_faq f LEFT JOIN t_user u ON u.id=f.created_by
           WHERE f.status='pending'
           ORDER BY f.id ASC LIMIT %s""", (limit,))


def review_faq(fid: int, approve: bool, user: dict,
               note: str = "") -> dict:
    """审核一条待审知识。approve=True 转 enabled 并进检索；
    approve=False 转 rejected（保留可追溯，不物理删除）。"""
    row = common.fetch_all("SELECT id, status FROM t_faq WHERE id=%s", (fid,))
    if not row:
        raise ValueError("这条知识不存在")
    if row[0]["status"] != "pending":
        raise ValueError("这条知识不是待审状态，不能重复审核")
    ts = schema.now()
    new_status = "enabled" if approve else "rejected"
    common.execute(
        "UPDATE t_faq SET status=%s, reviewed_by=%s, reviewed_at=%s, "
        "review_note=%s, updated_at=%s WHERE id=%s",
        (new_status, user["id"], ts, note[:250], ts, fid))
    if approve:
        faq.reload()
    return {"ok": True, "faq_id": fid, "status": new_status,
            "version": faq.version()}


# ---------------------------------------------------------------- 带单核查
#
# 治理板块以前挂着一个快捷问题「这个案子怎么判」。这句话有三个问题：
#   1. "这个案子"没有指代对象——窗口里根本没有选中任何案子；
#   2. 点下去必然被 case_judge 规则拦成"转人工"，用户看到的是"答不上来"；
#   3. 它和板块自己写的"不对在办案件下结论"直接打架。
# 运营真正需要的不是"判一下"，而是把一张具体的单摊开：事实是什么、
# 适用哪几条、剩下的交给仲裁员。所以这一格改成"挑一单查条款"，
# 点是先选单（真实案件列表），再让系统拼事实和条款原文。

def cases_for_ops(limit: int = 8) -> list[dict]:
    """在办案件优先，已结的放后面（复核申诉时也要能回看）。"""
    rows = common.fetch_all(
        """SELECT d.id, d.dispute_no, d.dtype, d.status, d.title, d.claim_amount,
                  d.order_no, d.priority, d.created_at, s.name AS shop_name
           FROM t_dispute d LEFT JOIN t_shop s ON s.id=d.shop_id
           ORDER BY CASE d.status WHEN 'pending_human' THEN 0
                                  WHEN 'arbitrating' THEN 1
                                  WHEN 'ai_review' THEN 2 ELSE 3 END,
                    d.priority DESC, d.id DESC LIMIT %s""", (limit,))
    return [{
        "id": int(r["id"]), "dispute_no": r["dispute_no"],
        "dtype": r.get("dtype") or "",
        "type_label": (gov.DTYPES.get(r.get("dtype") or "") or
                       gov.DTYPES["other"])["label"],
        "status": r.get("status") or "",
        "status_label": gov.STATUS_CN.get(r.get("status") or "", r.get("status") or ""),
        "title": (r.get("title") or "")[:60],
        "claim_amount": float(r.get("claim_amount") or 0),
        "order_no": r.get("order_no") or "",
        "shop_name": r.get("shop_name") or "",
        "created_at": str(r.get("created_at") or ""),
    } for r in rows]


def explain_case(sid: str, did: int) -> dict:
    """在治理会话里摊开一张案子。只写事实与条款，不写结论。"""
    sess = get_session(sid)
    if not sess:
        raise ValueError("会话不存在")
    if scope_of(sess) != "gov":
        raise ValueError("只有治理板块可以带单核查")
    brief = gov.case_brief(int(did))
    if not brief:
        raise ValueError("案件不存在")
    _push(sid, "bot", brief["text"], source="case")
    return {"reply": brief["text"], "source": "case", "handoff": False,
            "ticket_id": None, "score": 0, "faq_id": None, "intent": "带单核查",
            "dispute_id": brief["dispute_id"], "dispute_no": brief["dispute_no"],
            # 检索到的相关条款也结构化给前端：文本里已经有一份，
            # 但以后要做"点条款跳规则库原文"时，不该再从字符串里反解。
            "related": brief.get("related") or [],
            "latency_ms": 0,
            "trace": {"layers": [{"layer": "case_brief", "hit": True,
                                  "dispute_no": brief["dispute_no"],
                                  "note": "事实取订单库，条款取规则库原文，无模型参与"}]}}


# ---------------------------------------------------------------- 转平台介入
#
# 这条进线以前是断的：客服只能"答不上来 → 建工单"，纠纷只能"买家自己去
# 纠纷中心填表"。同一个人在同一个会话里说了十分钟，转到平台侧要全部重填一遍，
# 而且平台看到的是一段脱离上下文的诉求——这就是为什么"三方对接"喊了但没通。
#
# 现在统一成一条链：
#     客服会话（机器人/人工都在里面） → 一键转平台介入 → 纠纷单（带 session_id）
# 关键约束：
#   * 一个会话只对应一张未关闭的纠纷单。否则坐席连点两次就是两单，平台要判两遍。
#   * 对话内容自动成为诉求原文，不是让人再复述一遍。
#   * 证据只自动带「平台自己能核验」的那部分（聊天记录、订单），
#     买家的商品实拍之类必须真的上传过才算，不能靠转介凭空生成。
#   * 纠纷后续进展要回写到会话，否则买家在咨询窗口里永远停在一句"已转平台"。

# 一句话就能看出大概是什么纠纷。这里只做预填，最终类型由提交人确认：
# 猜错类型会让 AI 拿错条款去判，比不猜更糟。
_DTYPE_KW: list[tuple[str, list[str]]] = [
    ("safety", ["漏电", "起火", "爆炸", "受伤", "过敏", "人身安全"]),
    ("counterfeit", ["假货", "假冒", "山寨", "翻新", "高仿", "鉴定", "假一赔"]),
    ("fraud", ["被骗", "诈骗", "骗子", "站外", "私下转账", "加微信", "跑路"]),
    ("aftersale", ["售后", "退货", "换货", "退款申请", "不给退", "不处理", "没人管"]),
    ("shipping", ["没发货", "不发货", "物流", "快递", "运单", "没收到", "签收"]),
    ("description", ["描述不符", "货不对板", "和页面", "材质", "虚假宣传", "夸大", "图文不符"]),
    ("quality", ["质量", "破损", "坏了", "瑕疵", "开裂", "掉色", "不能用", "有味道"]),
    ("service", ["态度", "骂人", "辱骂", "没人理", "不回复", "推诿"]),
]


def guess_dtype(text: str) -> str:
    t = (text or "").lower()
    for key, kws in _DTYPE_KW:
        if any(k in t for k in kws):
            return key
    return "other"


def transcript(sid: str, limit: int = 20, width: int = 160) -> str:
    """把会话记录整理成一段可直接作为诉求原文的对话稿（时间正序）。"""
    rows = common.fetch_all(
        "SELECT role, content FROM t_cs_message WHERE session_id=%s AND content<>'' "
        "ORDER BY id DESC LIMIT %s", (sid, limit))
    rows.reverse()
    lab = {"buyer": "买家", "bot": "机器人", "agent": "客服", "system": "系统"}
    lines = []
    for r in rows:
        if r["role"] == "system":
            continue
        lines.append(f"{lab.get(r['role'], r['role'])}：{(r['content'] or '')[:width]}")
    return "\n".join(lines)


def dispute_of(sid: str) -> dict | None:
    """该会话已提交的纠纷（取最新一张未关闭的）。"""
    d = common.fetch_all(
        "SELECT d.id, d.dispute_no, d.dtype, d.status, d.stage, d.liability, d.title, "
        "d.claim_amount, d.ai_confidence, d.created_at, d.verdict_at "
        "FROM t_dispute d WHERE d.session_id=%s ORDER BY d.id DESC LIMIT 1", (sid,))
    if not d:
        return None
    x = d[0]
    return {**x, "id": int(x["id"]), "claim_amount": float(x.get("claim_amount") or 0),
            "ai_confidence": float(x.get("ai_confidence") or 0),
            "type_label": gov.DTYPES.get(x["dtype"], {}).get("label", x["dtype"]),
            "status_label": gov.STATUS_LABEL.get(x["status"], x["status"]),
            "created_at": str(x.get("created_at") or ""),
            "verdict_at": str(x.get("verdict_at") or "")}


def escalate(sid: str, user: dict, dtype: str = "", claim: str = "",
             claim_amount: float = 0, order_no: str = "", title: str = "",
             claim_type: str = "refund", evidence: list[dict] | None = None) -> dict:
    """把客服会话转成平台纠纷单。坐席和买家都能发起，幂等。"""
    sess = get_session(sid)
    if not sess:
        raise ValueError("会话不存在")
    if scope_of(sess) != "shop":
        # 商家问规则、运营问治理的会话不是交易纠纷，转过去只会给仲裁台添噪声，
        # 而且没有店铺、没有订单，判都判不了。
        raise ValueError("只有买家与店铺的客服会话可以转平台介入")
    shop_id = sess.get("shop_id")
    if not shop_id:
        raise ValueError("该会话没有关联店铺，无法申请平台介入")

    old = dispute_of(sid)
    if old and old["status"] != "closed":
        return {"exists": True, **old,
                "message": f"该会话已提交过平台介入（{old['dispute_no']}），沿用原单"}

    # 买家身份：优先会话绑定的账号（会话建立时写入），老会话没有 buyer_id 时
    # 只有买家自己发起才敢认定，坐席代转只能记昵称——否则纠纷会落到客服头上。
    bid = sess.get("buyer_id")
    if not bid and user.get("role") == "buyer":
        bid = user["id"]
    buyer = None
    if bid:
        rows = common.fetch_all(
            "SELECT id, display_name, shop_id FROM t_user WHERE id=%s", (bid,))
        buyer = dict(rows[0]) if rows else None
    if not buyer:
        buyer = {"id": None, "display_name": sess.get("buyer_name") or "买家",
                 "shop_id": None}

    text = transcript(sid)
    dtype = (dtype or "").strip() or guess_dtype(text + " " + (claim or ""))
    if dtype not in gov.DTYPES:
        dtype = "other"
    title = (title or "").strip() or f"{gov.DTYPES[dtype]['label']}：来自客服会话"
    claim = (claim or "").strip() or text or "买家在客服会话中要求平台介入"

    ev = [dict(e) for e in (evidence or []) if (e or {}).get("k")]
    have = {e["k"] for e in ev}
    # 会话记录本来就存在，平台可复核，属于真实凭证
    if "聊天记录" not in have:
        ev.append({"k": "聊天记录", "v": f"客服会话 {sid}（系统自动带入）"})
    # 订单由平台订单库直接核验，比买家自己截图更硬
    if order_no and "订单截图" not in have:
        ev.append({"k": "订单截图", "v": f"{order_no}（平台订单库核验）"})

    res = gov.create(int(shop_id), dtype, title, claim, float(claim_amount or 0),
                     ev, buyer, session_id=sid, order_no=order_no or "",
                     claim_type=claim_type or "refund")

    who = "商家代为" if user.get("role") == "merchant" else ""
    _push(sid, "system",
          f"已{who}提交平台介入：{res['dispute_no']} · {gov.DTYPES[dtype]['label']} · "
          f"诉求 {float(claim_amount or 0):.2f} 元\n平台受理后可在此页继续沟通", "dispute")
    common.execute("UPDATE t_cs_session SET status='escalated', handoff=1, updated_at=%s "
                   "WHERE id=%s", (schema.now(), sid))
    common.execute("UPDATE t_ticket SET status='done', closed_at=%s "
                   "WHERE session_id=%s AND status IN ('pending','processing')",
                   (schema.now(), sid))
    # 会话里的机器人提示要跟着变，否则买家还能继续问，问题分散在两个入口
    _push(sid, "system",
          "本次会话已转入平台纠纷流程，机器人暂停接待。后续沟通请在「纠纷中心」进行，"
          "平台仲裁员会在这里同步进展。", "dispute")

    out = {"exists": False, "session_id": sid, "buyer_bound": buyer["id"] is not None}
    out.update({k: res.get(k) for k in ("id", "dispute_no", "status", "liability",
                                        "confidence", "remedy", "amount", "points",
                                        "basis", "gate", "missing")})
    out["type_label"] = gov.DTYPES[dtype]["label"]
    out["dtype"] = dtype
    out["status_label"] = gov.STATUS_LABEL.get(res["status"], res["status"])
    return out


# ---------------------------------------------------------------- 指标

def metrics(shop_id: int | None = None) -> dict:
    """坐席台的指标。**只统计 scope=shop**：把商家问规则、运营问治理的会话
    算进"机器人独立解决率"，指标会虚高——那几个板块本来就没人接人工。"""
    where, args = ["s.scope='shop'"], []
    if shop_id:
        where.append("s.shop_id=%s")
        args.append(shop_id)
    w = " AND ".join(where)

    total = common.fetch_all(
        f"SELECT COUNT(*) AS n FROM t_cs_session s WHERE {w}", tuple(args))[0]["n"] or 0
    hand = common.fetch_all(
        f"SELECT COUNT(*) AS n FROM t_cs_session s WHERE {w} AND s.handoff=1",
        tuple(args))[0]["n"] or 0

    src = common.fetch_all(
        f"""SELECT m.source, COUNT(*) AS n FROM t_cs_message m
            JOIN t_cs_session s ON s.id=m.session_id
            WHERE m.role='bot' AND {w} GROUP BY m.source""", tuple(args))
    by_src = {(r["source"] or "unknown"): r["n"] for r in src}

    lat = common.fetch_all(
        f"""SELECT ROUND(AVG(m.latency_ms)) AS avg_ms FROM t_cs_message m
            JOIN t_cs_session s ON s.id=m.session_id
            WHERE m.role='bot' AND {w}""", tuple(args))[0]
    tk = common.fetch_all(
        "SELECT status, COUNT(*) AS n FROM t_ticket GROUP BY status")
    sc = common.fetch_all(
        f"SELECT s.scope, COUNT(*) AS n FROM t_cs_session s WHERE {w} GROUP BY s.scope",
        tuple(args))
    bots = sum(by_src.values())
    auto = by_src.get("faq", 0) + by_src.get("redis", 0) + by_src.get("llm", 0)
    return {
        "sessions": total,
        "handoff_sessions": hand,
        "bot_replies": bots,
        "by_source": by_src,
        "by_scope": {r["scope"]: r["n"] for r in sc},
        "auto_resolve_rate": round(auto / bots, 4) if bots else 0,
        "handoff_rate": round(hand / total, 4) if total else 0,
        "avg_latency_ms": lat["avg_ms"] or 0,
        "faq_hit_rate": round((by_src.get("faq", 0) + by_src.get("redis", 0)) / bots, 4) if bots else 0,
        "tickets": {r["status"]: r["n"] for r in tk},
        "ticket_pending": sum(r["n"] for r in tk if r["status"] == "pending"),
        # 坐席台默认看买家域（店里的话术库），三个域的规模单独给出来。
        # 买家域这里报**平台通用**口径：商家的看板不该把别家店的私有话术算进来。
        "faq": faq.index("buyer", None).stats(),
        "faq_by_audience": {a: faq.index(a, None).stats() for a in faq.AUDIENCES},
        "llm": get_llm().stats(),
        "redis": cache.stats(),
        "trend": _trend_7d(w, args),
    }


def _trend_7d(where: str, args: list) -> list[dict]:
    """近 7 日会话数（按天）。看板折线图用——没有时间序列就只能画个静态饼图。"""
    import datetime as _dt
    now = _dt.datetime.now()
    since = (now - _dt.timedelta(days=6)).strftime("%Y-%m-%d 00:00:00")
    rows = common.fetch_all(
        f"""SELECT substr(s.created_at,1,10) AS d, COUNT(*) AS n
            FROM t_cs_session s WHERE {where} AND s.created_at >= %s
            GROUP BY d ORDER BY d""", tuple(args + [since]))
    got = {r["d"]: r["n"] for r in rows}
    return [{"date": (now - _dt.timedelta(days=6 - i)).strftime("%m-%d"),
             "sessions": got.get((now - _dt.timedelta(days=6 - i)).strftime("%Y-%m-%d"), 0)}
            for i in range(7)]


# ---------------------------------------------------------------- 演示数据

DEMO = [
    ("橙子不加冰", "淘宝", "什么时候发货"),
    ("小满", "拼多多", "我要退款"),
    ("阿哲", "抖音", "你们家这个和图片颜色差好多，能换吗"),
    ("林小满", "淘宝", "能不能便宜点"),
    ("匿名买家", "拼多多", "我买的这个用了三天就坏了，我要投诉你们"),
]


def seed_demo(shop_id: int | None = None) -> int:
    """首次启动灌几条演示会话。空白的客服工作台没法演示，也看不出漏斗是否生效。"""
    if common.fetch_all("SELECT COUNT(*) AS n FROM t_cs_session")[0]["n"] > 0:
        return 0
    for name, ch, q in DEMO:
        s = new_session(shop_id, name, ch)
        try:
            handle(s["id"], q)
        except Exception as e:
            print("[cs] 演示会话生成失败:", str(e)[:80])
    return len(DEMO)
