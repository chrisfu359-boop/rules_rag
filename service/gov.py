"""平台监管：纠纷仲裁 + 商家处罚 + 申诉复核。

为什么需要这一层：
  店铺客服解决的是"我问你答"，解决不了"我不服"。买家对商家的处理不满意时，
  如果没有平台介入的出口，他只有两个选择：忍，或者去 12315/黑猫/社交平台曝光。
  后者对一个平台是实打实的合规风险。所以纠纷必须有第三方的、有约束力的裁决，
  而且裁决结果必须能落到商家身上（扣分、降权、停业），否则调解就是空话。

五级链路（越靠前越便宜，越靠后越贵）：

   ① 规则匹配       ~0ms   纠纷类型 → 违规条款 → 扣分标准，全部查表，模型不参与
   ② AI 定责       ~10ms  证据完整性 + 金额 + 历史违规 → 责任判定 + 置信度
   ③ AI 自动裁决    ~0ms   置信度 ≥ 0.80 且非严重违规 → 直接结案 + 生成处罚单
   ④ 人工仲裁       —      涉假/欺诈/人身安全/金额超限/证据冲突 → 仲裁员裁决
   ⑤ 申诉复核       —      商家申诉处罚、双方申诉裁决 → AI 初审 + 人工终审

三条不能破的边界：

  * **扣分不许由模型决定。** 处罚依据必须是条款库里真实存在的条款号，分数必须是
    条款里写明的分值（虚假宣传 2 分、售假 24 分……）。让模型"综合判断扣几分"，
    等于把行政处罚权交给一个会编造的组件——申诉时无法自证，监管检查时无法解释。
    模型在这里只做两件事：判断事实属于哪一类、把说理写成人话。

  * **AI 自动裁决只覆盖"低金额 + 一般违规"。** 涉假、欺诈、人身安全一律转人工，
    金额超过 1000 元一律转人工。这不是能力问题，是责任问题：错的代价不可逆。

  * **处罚必须可回滚。** 申诉成功要能把扣分退回去、把店铺状态恢复，
    并且留下"谁在什么时候因为什么撤销的"记录。只能加不能减的处罚系统，
    迟早会把一家店误杀，而且没人能证明是误杀。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time

import common
from api import schema
from generation.llm import get_llm
from service import logistics

# ---------------------------------------------------------------- 阈值

AUTO_CONF = float(os.getenv("GOV_AUTO_CONF", "0.80"))    # 自动裁决置信度下限
AUTO_MAX_AMOUNT = float(os.getenv("GOV_AUTO_MAX", "1000"))  # 自动裁决金额上限
APPEAL_DAYS = int(os.getenv("GOV_APPEAL_DAYS", "7"))     # 申诉窗口
APPEAL_REVIEW_HOURS = int(os.getenv("GOV_APPEAL_REVIEW_HOURS", "72"))  # 人工复核时限
APPEAL_MAX_SUPPLEMENT = int(os.getenv("GOV_APPEAL_MAX_SUPPLEMENT", "2"))  # 最多补充材料次数
SLA_HOURS = 24                                           # 转人工后的处理时限

# ---------------------------------------------------------------- 纠纷类型

# 每种纠纷对应的：事实类型、处理方案、扣分依据、需要哪些凭证。
# 扣分全部来自《商家违规行为管理规则》原文，条款号与分值一一对应：
#   2.2 虚假宣传 每次扣 2 分 / 2.3 售后超时 每次扣 1 分 / 2.4 发货超时 每单扣 0.5 分
#   2.5 虚假凭证 每次扣 4 分 / 3.1 售假 24 分 / 3.2 欺诈 48 分清退 / 3.3 危及人身安全 48 分清退
DTYPES: dict[str, dict] = {
    "quality": dict(
        label="商品质量问题", severity="normal", auto=True,
        remedy="return_refund", need=["商品实拍", "聊天记录"],
        clause="4.1", doc="商家售后服务管理规范", points=0.0,
        desc="商品存在破损、瑕疵、无法正常使用等质量问题"),
    "description": dict(
        label="描述不符 / 虚假宣传", severity="normal", auto=True,
        remedy="return_refund", need=["商品实拍", "商品详情截图"],
        clause="2.2", doc="商家违规行为管理规则", points=2.0,
        desc="实物与页面描述不符、夸大功效、虚构原价"),
    "shipping": dict(
        label="发货 / 物流问题", severity="normal", auto=True,
        remedy="refund", need=["订单截图"],
        clause="2.4", doc="商家违规行为管理规则", points=0.5,
        desc="超时未发货、虚假发货、物流停滞、丢件"),
    "aftersale": dict(
        label="售后不履行", severity="normal", auto=True,
        remedy="return_refund", need=["售后申请截图", "聊天记录"],
        clause="2.3", doc="商家违规行为管理规则", points=1.0,
        desc="商家拒绝或超时不处理退款、退货、换货申请"),
    "counterfeit": dict(
        label="假冒商品", severity="critical", auto=False,
        remedy="refund_compensate", need=["商品实拍", "检测报告"],
        clause="3.1", doc="商家违规行为管理规则", points=24.0,
        desc="销售假冒注册商标商品、盗版商品"),
    "fraud": dict(
        label="欺诈 / 诱导站外交易", severity="critical", auto=False,
        remedy="refund", need=["聊天记录"],
        clause="3.2", doc="商家违规行为管理规则", points=48.0,
        desc="诱导脱离平台交易、骗取财物"),
    "service": dict(
        label="服务态度", severity="normal", auto=True,
        remedy="apology", need=["聊天记录"],
        clause="", doc="", points=0.0,
        desc="辱骂、推诿、长时间不回复"),
    "safety": dict(
        label="商品危及人身安全", severity="critical", auto=False,
        remedy="refund_compensate", need=["检测报告", "医疗凭证"],
        clause="3.3", doc="商家违规行为管理规则", points=48.0,
        desc="商品存在危害人身安全的缺陷且商家隐瞒"),
    "other": dict(
        label="其他纠纷", severity="normal", auto=False,
        remedy="none", need=[], clause="", doc="", points=0.0,
        desc="无法归入上述类型的争议"),
}

EVIDENCE_TYPES = ["订单截图", "商品实拍", "商品详情截图", "聊天记录",
                  "物流记录", "开箱视频", "检测报告", "医疗凭证", "售后申请截图"]

# 每类纠纷的规则术语。单独一张表而不是塞进 DTYPES：
# DTYPES 是「给前端和裁决用的定义」，这里是「检索用的词表」，
# 两者会独立演化（比如新接一类纠纷要先改定义，词表可以后补）。
# 词必须来自规则原文的说法，不能用口语——BM25 索引的就是条款原文。
DTYPE_KW: dict[str, list[str]] = {
    "quality": ["质量问题", "瑕疵", "破损", "不符合质量要求", "三包", "修理更换退货"],
    "description": ["描述不符", "虚假宣传", "夸大", "误导", "与实物不符", "商品详情"],
    "shipping": ["发货", "虚假发货", "物流", "揽收", "运输", "交付"],
    "aftersale": ["售后服务", "退货", "退款", "换货", "逾期未处理", "拒绝"],
    "counterfeit": ["假冒", "注册商标", "盗版", "侵权", "假货"],
    "fraud": ["欺诈", "站外交易", "诱导", "骗取", "私下交易"],
    "service": ["服务态度", "辱骂", "推诿", "回复", "客户服务"],
    "safety": ["人身安全", "危害", "缺陷", "召回", "损害赔偿"],
    "other": ["争议", "平台介入", "处理", "责任"],
}

REMEDY_LABEL = {
    "refund": "仅退款", "return_refund": "退货退款", "exchange": "换货",
    "reship": "补寄", "refund_compensate": "退款并赔偿", "apology": "商家致歉整改",
    "reject": "驳回诉求", "none": "不涉及赔付",
}

PENALTY_LABEL = {
    "warning": "警告", "points": "违规扣分", "marketing_ban": "限制营销活动",
    "downgrade": "搜索降权", "publish_ban": "限制发布商品", "suspend": "暂停营业",
    "close": "店铺清退", "record": "责任认定（不扣分）",
}

# 这些处罚类型会改变店铺经营状态。record 不在其中：它是"认定商家有责但不扣分"
# 的留痕记录（比如服务态度类，规则库里没有对应扣分条款），不是处罚，
# 不能因为它把店铺状态改成"已警告"——那等于变相处罚，而且永远摘不掉。
STATUS_PENALTIES = {"warning", "points", "marketing_ban", "downgrade",
                    "publish_ban", "suspend", "close"}

# 扣分节点，对应《商家违规行为管理规则》4.2.1~4.2.4 原文。
# 顺序必须从高到低：一次扣 24 分会同时越过 2 分和 12 分两个节点，
# 只发最高的那个节点处罚，不然一次售假会同时被警告 + 限营销 + 降权，罚不当罪。
NODES = [
    (48, "close", "店铺清退", "累计扣满 48 分，依据 4.2.4 清退"),
    (24, "downgrade", "搜索降权 + 限制发布 7 天", "累计扣满 24 分，依据 4.2.3"),
    (12, "marketing_ban", "限制参加营销活动 30 天", "累计扣满 12 分，依据 4.2.2"),
    (2, "warning", "警告", "累计扣满 2 分，依据 4.2.1"),
]

# 处罚有效期（天）。没写在这里的类型 = 无限期（只有 close 清退）。
#
# 这里曾经只有三个类型，warning 和 suspend 没有期限，结果是：
#   1) 节点警告发出去 expire_at 为 NULL，_active_penalties 永远算它在期内，
#      店铺状态就永远钉在「已警告」，没有任何人能把它改回来；
#   2) 手工「暂停营业」不填天数时同样无期限，等于变成清退。
# 处罚必须有头有尾 —— 期限就是「整改完成后恢复」这句话的落地方式。
PENALTY_DAYS = {"warning": 30, "marketing_ban": 30, "downgrade": 30,
                "publish_ban": 7, "suspend": 7}
NODE_DAYS = PENALTY_DAYS          # 旧名保留：调用点还按节点天数理解
STATUS_LABEL = {"normal": "正常", "warned": "已警告", "restricted": "受限",
                "downgraded": "降权中", "suspended": "暂停营业", "closed": "已清退"}

# 会改变店铺经营状态的处罚类型。续期/到期判断都按这一组来，
# points 只影响分数不影响状态，不能算进来（否则扣一分就永远"非 normal"）。
STATE_TYPES = ("warning", "marketing_ban", "downgrade", "publish_ban",
               "suspend", "close")

# 纠纷状态的中文名。原来只在两个页面的 JS 里各写了一份，
# 服务端这次要在「带单核查」的回复里用它，就统一下沉到这里——
# 三处各写一遍的结果是同一张单在三个页面叫三个名字。
STATUS_CN = {"ai_review": "AI 初审", "pending_human": "待裁决", "arbitrating": "仲裁中",
             "resolved": "已裁决", "appealing": "申诉中", "closed": "已关闭"}


# ---------------------------------------------------------------- 工具

def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse(v) -> dt.datetime | None:
    """MySQL 返回 datetime，SQLite 返回字符串，两种都得能算时差。"""
    s = str(v or "")[:19]
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def _plus(days: float = 0, hours: float = 0) -> str:
    return (dt.datetime.now() + dt.timedelta(days=days, hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S")


def _json(v, default):
    if not v:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def next_no() -> str:
    day = dt.date.today().strftime("%Y%m%d")
    n = common.fetch_all("SELECT COUNT(*) AS n FROM t_dispute WHERE dispute_no LIKE %s",
                         (f"DP{day}%",))[0]["n"] or 0
    return f"DP{day}{int(n) + 1:04d}"


def evidence_of(d: dict) -> list[dict]:
    out = _json(d.get("evidence"), [])
    return [e for e in out if isinstance(e, dict)]


def parse_evidence(v) -> list[dict]:
    """给接口层用：把库里的 JSON 字符串还原成结构化凭证。"""
    out = _json(v, [])
    return [e for e in out if isinstance(e, dict)]


def _keys(d: dict) -> set[str]:
    return {str(e.get("k") or "").strip() for e in evidence_of(d)}


def get(dispute_id: int) -> dict | None:
    rows = common.fetch_all("SELECT * FROM t_dispute WHERE id=%s", (dispute_id,))
    return rows[0] if rows else None


def credit(shop_id: int) -> dict:
    rows = common.fetch_all("SELECT * FROM t_shop_credit WHERE shop_id=%s", (shop_id,))
    if rows:
        return rows[0]
    common.execute("INSERT INTO t_shop_credit (shop_id, credit_score, level, updated_at) "
                   "VALUES (%s,100,'良好',%s)", (shop_id, _now()))
    return common.fetch_all("SELECT * FROM t_shop_credit WHERE shop_id=%s", (shop_id,))[0]


def add_msg(did: int, role: str, content: str, kind: str = "text", meta: str = "") -> int:
    mid = common.insert(
        "INSERT INTO t_dispute_msg (dispute_id, role, content, kind, meta, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)", (did, role, content, kind, meta[:480], _now()))
    common.execute("UPDATE t_dispute SET updated_at=%s WHERE id=%s", (_now(), did))
    return mid


def _notify_cs(d: dict, text: str) -> None:
    """把纠纷进展推回来源客服会话（会转纠纷的那个会话）。

    不 import service.cs 复用 _push：cs 依赖 gov（new_session 要查 shop_status），
    反向导入就成环了。这里只写两张表，口径和 cs._push 保持一致。
    没有 session_id 的纠纷（买家直接在纠纷中心提交）什么都不做。
    """
    sid = str((d or {}).get("session_id") or "").strip()
    if not sid or not text:
        return
    if not common.fetch_all("SELECT id FROM t_cs_session WHERE id=%s", (sid,)):
        return
    ts = _now()
    common.execute(
        "INSERT INTO t_cs_message (session_id, role, content, source, score, faq_id, "
        "intent, latency_ms, created_at) VALUES (%s,'system',%s,'dispute',0,NULL,'',0,%s)",
        (sid, text[:500], ts))
    common.execute("UPDATE t_cs_session SET msg_count=msg_count+1, last_msg=%s, "
                   "updated_at=%s WHERE id=%s", (text[:250], ts, sid))


def _notify_dispute(dispute_id: int, text: str) -> None:
    d = get(dispute_id)
    if d:
        _notify_cs(d, text)


# ---------------------------------------------------------------- 店铺状态

def _active_penalties(shop_id: int) -> list[dict]:
    rows = common.fetch_all(
        "SELECT * FROM t_penalty WHERE shop_id=%s AND status='active' "
        "ORDER BY id DESC", (shop_id,))
    now = dt.datetime.now()
    out = []
    for r in rows:
        exp = _parse(r.get("expire_at"))
        if exp and exp < now:
            continue          # 过期不续罚，但记录保留（申诉时还要查）
        out.append(r)
    return out


def refresh_shop(shop_id: int) -> dict:
    """由**在罚单**推导店铺状态，而不是直接写死一个 status 字段。

    这里踩过一类典型坑：处罚时把 status 设成 suspended，处罚到期后没人去改回来，
    店铺永远停着。状态是衍生物，就必须每次从事实重新算一遍。
    """
    c = credit(shop_id)
    pens = [p for p in _active_penalties(shop_id) if p["ptype"] in STATUS_PENALTIES]
    types = {p["ptype"] for p in pens}
    points = float(common.fetch_all(
        "SELECT COALESCE(SUM(points),0) AS p FROM t_penalty "
        "WHERE shop_id=%s AND status='active'", (shop_id,))[0]["p"] or 0)

    if "close" in types:
        status = "closed"
    elif "suspend" in types:
        status = "suspended"
    elif "publish_ban" in types or "downgrade" in types:
        status = "downgraded"
    elif "marketing_ban" in types:
        status = "restricted"
    elif "warning" in types:
        status = "warned"
    else:
        status = "normal"

    score = max(0, min(100, int(round(100 - points * 0.8))))
    if score >= 90:
        level = "优秀"
    elif score >= 75:
        level = "良好"
    elif score >= 60:
        level = "一般"
    elif score >= 40:
        level = "风险"
    else:
        level = "高危"

    sus = common.fetch_all(
        "SELECT MAX(expire_at) AS e FROM t_penalty WHERE shop_id=%s AND status='active' "
        "AND ptype='suspend'", (shop_id,))[0]["e"]
    hist_n = common.fetch_all("SELECT COUNT(*) AS n FROM t_penalty WHERE shop_id=%s",
                              (shop_id,))[0]["n"] or 0
    common.execute(
        """UPDATE t_shop_credit SET credit_score=%s, level=%s, points_year=%s,
               status=%s, suspend_until=%s, penalty_count=%s, updated_at=%s
           WHERE shop_id=%s""",
        (score, level, round(points, 2), status, sus, hist_n, _now(), shop_id))
    # 注意字段名：这里返回的是**店铺累计分**，不是本次扣分。
    # 曾经两边都叫 points，punish() 用 **state 合并时把本次扣分值悄悄覆盖成了累计值，
    # 接口返回"扣 3 分"而实际只扣了 1 分——不报错，但对外数字是错的。
    return {"shop_id": shop_id, "status": status, "status_label": STATUS_LABEL.get(status, status),
            "credit_score": score, "level": level, "shop_points": round(points, 2)}


def shop_status(shop_id: int | None) -> str:
    """店铺当前经营状态。读的时候就顺手把「到期没回收」的状态修回来。

    原来的写法是直接读 t_shop_credit.status。而 status 只在处罚/撤销时被
    refresh_shop 重算过，一旦罚单到期，没有任何事件会触发重算，于是：
    罚单早过期了，店铺还挂着「暂停营业」，客服拒绝接待、买家申请不了售后。
    状态是罚单的衍生物，就不能只在写入时算一次——读取路径上要能自愈。
    """
    if not shop_id:
        return "normal"
    rows = common.fetch_all("SELECT status FROM t_shop_credit WHERE shop_id=%s", (shop_id,))
    st = (rows[0]["status"] if rows else "normal") or "normal"
    if st == "normal":
        return st
    # 还有状态类处罚在期内吗？有就沿用，没有说明是过期残留，重算。
    live = common.fetch_all(
        "SELECT COUNT(*) AS n FROM t_penalty WHERE shop_id=%s AND status='active' "
        f"AND ptype IN ({','.join(['%s'] * len(STATE_TYPES))}) "
        "AND (expire_at IS NULL OR expire_at > %s)",
        (shop_id, *STATE_TYPES, _now()))[0]["n"]
    if live:
        return st
    return (refresh_shop(shop_id).get("status") or "normal")


def expire_due(limit: int = 500) -> dict:
    """把已到期的罚单从 active 改成 expired，并重算受影响店铺的状态。

    为什么要真的改 status 而不是只在查询时过滤：
      · 「在罚单」是仲裁、申诉、监管报表三处共用的口径，靠每个查询各自
        加时间条件，迟早有一处漏掉，漏掉的那处就会把过期处罚算进去；
      · 罚单是账本，过期不该消失（申诉时要查），所以是标记 expired 而不是删。
    返回处理了几张、恢复了哪些店铺，供定时任务和手动触发共用。
    """
    now = _now()
    rows = common.fetch_all(
        "SELECT id, shop_id, ptype FROM t_penalty WHERE status='active' "
        "AND expire_at IS NOT NULL AND expire_at <= %s ORDER BY id LIMIT %s",
        (now, max(1, int(limit))))
    if not rows:
        return {"expired": 0, "shops": [], "recovered": []}
    ids = [int(r["id"]) for r in rows]
    shops = sorted({int(r["shop_id"]) for r in rows})
    common.execute(
        f"UPDATE t_penalty SET status='expired' WHERE id IN ({','.join(['%s'] * len(ids))})",
        tuple(ids))
    recovered = []
    before = {}
    for sid in shops:
        c = credit(sid)
        before[sid] = (c.get("status") or "normal")
        st = (refresh_shop(sid).get("status") or "normal")
        if st != before[sid]:
            recovered.append({"shop_id": sid, "from": before[sid], "to": st})
    # 恢复营业是要留痕的：谁都没点过按钮，店铺却从停业变回正常，
    # 事后核查必须有据可查，否则会被当成越权改状态。
    if recovered:
        _audit(0, "penalty_expire",
               ",".join(f"shop:{r['shop_id']}" for r in recovered),
               f"到期自动恢复 {len(recovered)} 家；罚单 {len(ids)} 张")
    return {"expired": len(ids), "shops": shops, "recovered": recovered}


def _audit(user_id: int | None, action: str, target: str = "", detail: str = "") -> None:
    """service 层直写审计表。不 import api.schema：service 不该反向依赖 api 层，
    而 api/schema.py 又要 import common —— 绕回去就是循环导入。
    审计失败不影响主流程（理由同 api/schema.audit）。"""
    try:
        common.execute(
            "INSERT INTO t_audit_log (user_id, action, target, ip, detail, created_at) "
            "VALUES (%s,%s,%s,'',%s,%s)",
            (user_id, action, target[:120], detail[:480], _now()))
    except Exception:
        pass


# ---------------------------------------------------------------- 处罚

def _mk_penalty(shop_id: int, dispute_id: int | None, ptype: str, points: float,
                node: str, reason: str, clause_no: str, basis: str,
                auto: bool, operator: int | None, expire_at: str | None = None) -> int:
    return common.insert(
        """INSERT INTO t_penalty (shop_id, dispute_id, ptype, points, node, reason, basis,
               clause_no, status, auto, operator_id, effective_at, expire_at, created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s)""",
        (shop_id, dispute_id, ptype, round(points, 2), node, reason[:250], basis[:250],
         clause_no[:60], 1 if auto else 0, operator,
         _now(), expire_at, _now()))


def punish(shop_id: int, dispute_id: int | None, points: float, clause_no: str,
           reason: str, auto: bool = True, operator: int | None = None,
           basis: str = "") -> dict:
    """扣分 → 累计 → 跨节点 → 生成节点处罚。返回罚单信息。

    一次违规可能同时越过多个节点（售假 24 分一次越过 2 分和 12 分）。
    只发最高的那一个，理由见 NODES 注释：处罚要"罚当其罪"，
    不能因为算法写错了把一次违规叠加成三条处罚。
    """
    c = credit(shop_id)
    before = float(c["points_year"] or 0)
    after = before + float(points or 0)

    pid = None
    if points:
        pid = _mk_penalty(shop_id, dispute_id, "points", points, "", reason,
                          clause_no, basis or "扣分依据见违规条款", auto, operator)

    crossed = None
    for node, ptype, label, why in NODES:
        if before < node <= after:
            crossed = (node, ptype, label, why)
            break          # NODES 从高到低，第一个命中的就是最高节点

    node_pid = None
    if crossed:
        node, ptype, label, why = crossed
        days = NODE_DAYS.get(ptype)
        node_pid = _mk_penalty(
            shop_id, dispute_id, ptype, 0, node,
            f"{label}：{why}", "", why,
            auto, operator, _plus(days=days) if days else None)
        if dispute_id:
            add_msg(dispute_id, "system",
                    f"触发扣分节点：{label}（累计 {round(after, 1)} 分）", "penalty")

    state = refresh_shop(shop_id)
    return {"penalty_id": pid, "node_penalty_id": node_pid,
            "points": round(float(points or 0), 2), "points_total": round(after, 2),
            "node": (crossed[0] if crossed else None),
            "node_label": (crossed[2] if crossed else ""), **state}


def revoke_penalty(penalty_id: int, by: int | None, reason: str) -> dict:
    """撤销罚单。扣分不是删记录，是标记 revoked —— 罚过、撤过都得能查。

    撤销一张扣分单时，必须连带撤销它触发的节点处罚。否则会出现这种荒谬状态：
    扣分清零了，店铺却因为"累计扣满 2 分"的警告单还在生效，一直挂着 warned。
    处罚是链式的，回滚就必须是链式的。
    """
    rows = common.fetch_all("SELECT * FROM t_penalty WHERE id=%s", (penalty_id,))
    if not rows:
        raise ValueError("处罚单不存在")
    p = rows[0]
    if p["status"] != "active":
        return {"ok": True, "already": p["status"]}
    common.execute(
        "UPDATE t_penalty SET status='revoked', revoked_at=%s WHERE id=%s",
        (_now(), penalty_id))

    cascaded = []
    if p["ptype"] == "points" and p.get("dispute_id"):
        for sib in common.fetch_all(
                "SELECT id FROM t_penalty WHERE shop_id=%s AND dispute_id=%s "
                "AND status='active' AND node<>''", (p["shop_id"], p["dispute_id"])):
            common.execute("UPDATE t_penalty SET status='revoked', revoked_at=%s "
                           "WHERE id=%s", (_now(), sib["id"]))
            cascaded.append(int(sib["id"]))

    if p["dispute_id"]:
        add_msg(int(p["dispute_id"]), "system",
                f"处罚单 #{penalty_id} 已撤销：{reason[:200]}"
                + (f"（连带撤销节点处罚 {cascaded}）" if cascaded else ""), "penalty")
    state = refresh_shop(int(p["shop_id"]))
    return {"ok": True, "revoked": penalty_id, "cascaded": cascaded, **state}


def manual_penalty(shop_id: int, ptype: str, days: int | None, reason: str,
                   operator: int, dispute_id: int | None = None) -> dict:
    """仲裁员的裁量处罚（停业整顿、限制发布等）。走同一张罚单表，
    这样"店铺现在为什么停着"永远只有一个答案来源。

    不填天数时用该类型的默认有效期，而不是无限期：手动处罚最常被点的是
    「暂停营业」，不填天数就变成永久停业，和清退没区别，而且再没人会想起来解封。
    """
    if ptype not in PENALTY_LABEL:
        raise ValueError("未知的处罚类型")
    if ptype == "record":
        raise ValueError("责任认定不是处罚，不能生成罚单")
    # points（违规扣分）只扣分、不改变经营状态、无期限，不需要天数；
    # close（清退）永久生效也无期限。其余会改状态的类型，没填天数就用默认有效期。
    if ptype in ("points", "close"):
        d = None
    else:
        d = days if days else PENALTY_DAYS.get(ptype)
        if not d:
            raise ValueError("该处罚类型必须指定天数")
    exp = _plus(days=d) if d else None
    pid = _mk_penalty(shop_id, dispute_id, ptype, 0, "", reason, "", "",
                      False, operator, exp)
    state = refresh_shop(shop_id)
    if dispute_id:
        add_msg(dispute_id, "system",
                f"平台对店铺执行处罚：{PENALTY_LABEL[ptype]}"
                + (f"（{d} 天，{exp} 到期）" if d else "（长期）"), "penalty")
    return {"penalty_id": pid, "days": d, "expire_at": exp, **state}


# ---------------------------------------------------------------- AI 调解员

def _order(order_no: str) -> dict | None:
    if not order_no:
        return None
    rows = common.fetch_all("SELECT * FROM t_order WHERE order_no=%s", (order_no,))
    return rows[0] if rows else None


def _order_items(o: dict) -> list[dict]:
    """订单商品行：子表优先，子表为空才回退到主表单行。

    兜底不是过渡方案，而是长期策略。t_order 上的 item_title 还写着手工造数的
    脚本、老用例、列表页的快速查询，子表只承载「多商品」这一件它做不到的事。
    两套数据都可能为真，就必须有明确优先级：有子表行信子表，没有才回退——
    这样上线不需要批量改存量数据，也不会出现半单走子表半单走主表的错乱。
    """
    no = str(o.get("order_no") or "")
    rows = common.fetch_all(
        "SELECT line_no, title, sku, qty, unit_price, refund_qty, refund_amount "
        "FROM t_order_item WHERE order_no=%s ORDER BY line_no", (no,)) if no else []
    out = []
    for r in rows:
        q = int(r.get("qty") or 0)
        u = float(r.get("unit_price") or 0)
        out.append({"line_no": int(r.get("line_no") or 1),
                    "title": r.get("title") or "", "sku": r.get("sku") or "",
                    "qty": q, "unit_price": u, "total": round(q * u, 2),
                    "refund_qty": int(r.get("refund_qty") or 0),
                    "refund_amount": round(float(r.get("refund_amount") or 0), 2)})
    if out:
        return out
    t = (o.get("item_title") or "").strip()
    if not t:
        return []
    q = int(o.get("qty") or 1) or 1
    u = float(o.get("unit_price") or 0)
    return [{"line_no": 1, "title": t, "sku": o.get("sku") or "",
             "qty": q, "unit_price": u, "total": round(q * u, 2),
             "refund_qty": 0, "refund_amount": 0.0}]


# 时效标准：写在这里而不是散在前端，因为 AI 定责和页面展示必须用同一把尺子。
SHIP_SLA_HOURS = 48      # 承诺发货时效（规则 2.4）
AFTERSALE_SLA_HOURS = 36  # 售后审核时限（售后服务管理规范 2.3.3）
PICK_SLA_HOURS = 24      # 发货后多久必须有揽收记录（规则 2.4 虚假发货的判定口径）


def order_detail(order_no: str) -> dict | None:
    """订单快照 + 时效计算，三方看的是同一份事实。

    平台仲裁最怕「各说各话」：买家说三天没发货，商家说当天就出库了。
    唯一能对齐双方的事实来源是订单时间戳，所以这份快照不是给平台看的
    「内部资料」，而是买家、商家、平台三端同源显示的公共事实底座。
    页面上的「超时」结论也必须从这里算，不能由 AI 自由发挥。

    v8 补了两类此前完全缺失的事实：
      * 商品明细（买了什么、几件、单价）——只有金额的订单，判「描述不符」时
        连买家说的「实木底座」指的是哪件商品都对不上。
      * 物流（承运商、运单号、揽收时间）——规则 2.4 写着「上传无效运单号按虚假
        发货扣 4 分」，可订单表连运单号都没有，这条规则在系统里根本无法落地。
    """
    o = _order(order_no or "")
    if not o:
        return None
    paid = _parse(o.get("paid_at"))
    shipped = _parse(o.get("shipped_at"))
    recv = _parse(o.get("received_at"))
    picked = _parse(o.get("picked_at"))
    applied = _parse(o.get("aftersale_applied_at"))
    now = dt.datetime.now()

    def gap(a, b) -> float | None:
        if not a:
            return None
        return round(((b or now) - a).total_seconds() / 3600, 1)

    ship_h = gap(paid, shipped)
    recv_h = gap(shipped, recv)
    wait_h = gap(applied, None)

    qty = int(o.get("qty") or 1) or 1
    unit = float(o.get("unit_price") or 0)
    amount = float(o.get("amount") or 0)
    items = _order_items(o)
    subtotal = round(sum(i["total"] for i in items), 2)
    tracking = (o.get("tracking_no") or "").strip()
    # 揽收时间优先查物流轨迹接口（真实事实），静态 picked_at 只作回退：
    # 老订单造数时已经写死了 picked_at，但新口径要求「运单号 → 轨迹 → 揽收」，
    # 否则 fake_ship 永远是「读写死的字段验证写死的结论」。
    carrier = o.get("carrier") or ""
    trace = logistics.get_trace(carrier, tracking, str(o.get("shipped_at") or ""),
                                str(o.get("picked_at") or ""))
    live_pick = logistics.pick_time(carrier, tracking, str(o.get("shipped_at") or ""),
                                    str(o.get("picked_at") or ""))
    eff_pick = _parse(live_pick) or picked          # 轨迹揽收 > 静态揽收
    # 揽收耗时只算「发货 → 首次揽收」这一段：包裹真正进入承运方网络才算发货完成。
    # 商家只点了一下「发货」按钮、物流公司没来取件，对买家而言等于没发。
    pick_h = gap(shipped, eff_pick) if (shipped and eff_pick) else None
    pick_wait = gap(shipped, None) if (shipped and not eff_pick) else None
    no_tracking = bool(shipped and not tracking)
    # 虚假发货：说发了，但要么没有运单号，要么过了 24 小时物流还没揽收。
    # 已签收的不判——包裹最终到了，就不属于「用虚假物流信息规避发货义务」。
    fake_ship = bool(shipped and not recv and
                     (no_tracking or (pick_wait is not None and pick_wait > PICK_SLA_HOURS)))

    return {
        "order_no": o["order_no"],
        "shop_name": o.get("shop_name") or "",
        "category": o.get("category") or "",
        "amount": amount,
        "status": o.get("status") or "",
        "paid_at": str(o.get("paid_at") or ""),
        "shipped_at": str(o.get("shipped_at") or ""),
        "received_at": str(o.get("received_at") or ""),
        "aftersale_type": o.get("aftersale_type") or "",
        "aftersale_applied_at": str(o.get("aftersale_applied_at") or ""),
        "remarks": o.get("remarks") or "",
        # ---- 商品明细
        # items 是主口径（支持多商品、部分退款），item 是首行的别名，
        # 保留它是因为前端和已有用例都按「一单一品」写的，改口径不能顺手把
        # 所有读取点一起改崩——两个字段同时给，先让新页面用 items。
        "items": items,
        "item": items[0] if items else {"title": "", "sku": "", "qty": 0,
                                        "unit_price": 0.0, "total": 0.0,
                                        "refund_qty": 0, "refund_amount": 0.0},
        "item_count": len(items),
        "qty_total": sum(i["qty"] for i in items),
        "subtotal": subtotal,
        # 商品小计与实付不一致，说明有优惠/运费/部分退款，退款时不能直接按实付全退
        "amount_mismatch": bool(subtotal and abs(round(subtotal - amount, 2)) > 0.01),
        # 已退数量：部分退款后「这件还能不能退」要看它，不能只看整单状态
        "refunded_qty": sum(i["refund_qty"] for i in items),
        "refunded_amount": round(sum(i["refund_amount"] for i in items), 2),
        # ---- 物流
        "logistics": {"carrier": carrier, "tracking_no": tracking,
                      "picked_at": str(eff_pick or ""),
                      "pick_hours": pick_h, "pick_wait_hours": pick_wait,
                      "no_tracking": no_tracking, "fake_ship": fake_ship,
                      "trace": trace},
        "ship_hours": ship_h,
        "recv_hours": recv_h,
        "aftersale_wait_hours": wait_h,
        "ship_late": bool(ship_h is not None and ship_h > SHIP_SLA_HOURS),
        "ship_open": bool(paid and not shipped),
        "aftersale_late": bool(wait_h is not None and wait_h > AFTERSALE_SLA_HOURS),
        "fake_ship": fake_ship,
        "no_tracking": no_tracking,
        "pick_sla": PICK_SLA_HOURS,
        "ship_sla": SHIP_SLA_HOURS,
        "aftersale_sla": AFTERSALE_SLA_HOURS,
    }


def warm_clause_index() -> int:
    """启动预热。406 条条款走 jieba 建索引要 1~2 秒，不能放在第一个运营点开时付。"""
    _clause_index()
    return int(_CLAUSE_CI["n"] or 0)


_CLAUSE_CI: dict = {"at": 0.0, "idx": None, "n": 0}


def _clause_index():
    """条款库的 BM25 索引（懒加载 + 10 分钟缓存）。

    为什么在治理服务里再建一个索引，而不是复用对话链路的那个：
      对话链路的检索器带着过滤/精排/缓存，是给"回答问题"用的；
      这里要的是「给定一段事实，把最接近的条款原文捞出来」，输入是一段
      没有问句结构的描述，两者共用反而要迁就彼此。406 条条款建索引只要
      几十毫秒，各建各的更清楚。
    """
    if _CLAUSE_CI["idx"] is not None and time.time() - _CLAUSE_CI["at"] < 600:
        return _CLAUSE_CI["idx"]
    try:
        rows = common.load_clauses()
        from retrieval.bm25 import BM25
        idx = BM25(rows)
    except Exception as e:
        print("[gov] 条款索引构建失败，带单核查降级为单条映射:", str(e)[:80])
        return None
    _CLAUSE_CI.update(at=time.time(), idx=idx, n=len(rows))
    return idx


# 程序性章节的识别词。这些章节（申诉、举证、违规分级、总则）里会**提到**违规行为，
# 但不是在**规定**它：搜"假冒"会命中「严重违规指涉及假冒商品……」，搜"退货"会命中
# 「申诉应提交……退货凭证」。它们在 BM25 里分数不低，因为确实出现了这些词，
# 但对"这一单该按哪条判"没有用。按章节名降权，比加一堆硬编码条款号更耐改版。
GENERIC_PATH = ("申诉", "举证", "证据", "违规分级", "总则", "定义", "适用范围",
                "生效", "解释", "附则", "违规处理流程")


def _related_clauses(d: dict, t: dict, order_title: str = "",
                     category: str = "", limit: int = 3) -> list[dict]:
    """按本案事实在**全量条款**里检索相关条文，返回原文而不是结论。

    原来这里只有一条硬映射（类型 → 条款号），406 条条款里实际被用到的只有 9 条：
    同一类型的案子，不管事实差多远，看到的依据永远那一句。
    「挑一单查条款」的价值恰恰在于把**这一单**的细节（商品、物流、金额）
    带进检索，让没被硬映射覆盖的条款也能浮出来。
    """
    idx = _clause_index()
    if not idx:
        return []
    q = " ".join(x for x in [
        t.get("label", ""), t.get("desc", ""), " ".join(DTYPE_KW.get(d.get("dtype"), [])),
        d.get("title") or "", (d.get("claim") or "")[:80],
        d.get("aftersale_type") or "", order_title, category,
    ] if x)
    try:
        hits = idx.search(q, top_k=limit + 5)
    except Exception:
        return []
    if not hits:
        return []
    kws = DTYPE_KW.get(d.get("dtype"), [])
    ranked = []
    for row, score in hits:
        s = float(score or 0)
        path = str(row.get("clause_path") or "")
        body = str(row.get("content") or "")
        # 章节名里就有该行为的（2.4 发货超时 / 3.1 售假），才是"规定"它；
        # 正文里顺带提到的，权重往下压。
        if any(k and k in path for k in kws):
            s *= 1.45
        elif any(k and k in body[:40] for k in kws):
            s *= 1.05
        if any(g in path for g in GENERIC_PATH):
            s *= 0.45
        ranked.append((row, s))
    ranked.sort(key=lambda x: -x[1])
    top = float(ranked[0][1] or 0)
    floor = max(4.0, top * 0.30)
    used = {(t.get("doc"), t.get("clause"))}
    out, seen = [], set()
    for row, score in ranked:
        key = (row.get("doc_name"), row.get("clause_no"))
        if key in used or key in seen:
            continue                      # 已在【适用条款】里给过原文的，不再重复
        if float(score or 0) < floor:
            continue
        seen.add(key)
        out.append({"doc": row.get("doc_name") or "", "clause": row.get("clause_no") or "",
                    "path": row.get("clause_path") or "",
                    "content": " ".join((row.get("content") or "").split()),
                    "scene": row.get("scene") or "",
                    "score": round(float(score), 1)})
        if len(out) >= limit:
            break
    return out


def case_brief(did: int) -> dict | None:
    """把一张案子摊成「事实 + 适用条款」，**不给结论**。

    治理板块的定位是"只给依据和口径，不对在办案件下结论"，所以运营点
    「挑一单查条款」时，系统要做的是把这张单的事实和对应条款摆出来，
    而不是让大模型写一句"应当由商家承担"——那句话会被截图当裁决书用。

    这里刻意不用大模型：事实来自订单时间戳，条款来自规则库原文，
    两者都是可核验的字符串，拼装不需要推理，也就不存在幻觉。
    """
    rows = common.fetch_all(
        """SELECT d.*, s.name AS shop_name FROM t_dispute d
           LEFT JOIN t_shop s ON s.id=d.shop_id WHERE d.id=%s""", (did,))
    if not rows:
        return None
    d = rows[0]
    t = DTYPES.get(d.get("dtype")) or DTYPES["other"]
    o = order_detail(d.get("order_no") or "")

    # 条款原文：先按类型指定的文档找，找不到再跨文档按条款号找（同一小节号
    # 在不同规范里都有，比如 3.1，只按号找会拿到不相干的那条）。
    cl, doc = t["clause"], t["doc"]
    clause_rows = []
    if cl:
        clause_rows = common.fetch_all(
            "SELECT c.clause_no, c.content, doc.doc_name FROM t_clause c "
            "JOIN t_rule_doc doc ON doc.id=c.doc_id WHERE c.clause_no=%s AND doc.doc_name=%s "
            "ORDER BY c.id LIMIT 1", (cl, doc))
        if not clause_rows:
            clause_rows = common.fetch_all(
                "SELECT c.clause_no, c.content, doc.doc_name FROM t_clause c "
                "JOIN t_rule_doc doc ON doc.id=c.doc_id WHERE c.clause_no=%s "
                "ORDER BY c.id LIMIT 1", (cl,))

    def h(v) -> str:
        return "—" if v is None else (f"{v / 24:.1f} 天" if v >= 24 else f"{v:.1f} 小时")

    lines = [f"【案件】{d['dispute_no']} · {t['label']} · "
             f"{STATUS_CN.get(d.get('status'), d.get('status') or '')}"]
    lines.append(f"【双方】店铺：{d.get('shop_name') or '—'} / 买家：{d.get('buyer_name') or '—'}")
    lines.append(f"【诉求】{(d.get('title') or '')[:60]}　要求退款 ¥{float(d.get('claim_amount') or 0):.2f}")

    if o:
        items = o["items"]
        head = f"【订单】{o['order_no']}"
        if len(items) == 1:
            it = items[0]
            if it["title"]:
                head += f" · {it['title']}"
                if it["sku"]:
                    head += f"（{it['sku']}）"
                head += f" · {it['qty']} 件 × ¥{it['unit_price']:.2f}"
            head += f" · 实付 ¥{o['amount']:.2f}"
            lines.append(head)
            if it["refund_qty"]:
                lines.append(f"【已退】{it['title']} 已退 {it['refund_qty']} 件 / "
                             f"¥{it['refund_amount']:.2f}")
        elif items:
            head += f" · 共 {o['qty_total']} 件 / {len(items)} 种商品 · 实付 ¥{o['amount']:.2f}"
            lines.append(head)
            # 多商品必须逐行列：退款争议常常只针对其中一件，
            # 只给一个总金额的话，仲裁员没法判断"退哪件、退多少"。
            for i, it in enumerate(items, 1):
                seg = f"  {i}. {it['title']}"
                if it["sku"]:
                    seg += f"（{it['sku']}）"
                seg += f" ×{it['qty']}　¥{it['unit_price']:.2f}"
                if it["refund_qty"]:
                    seg += f"　已退 {it['refund_qty']} 件 / ¥{it['refund_amount']:.2f}"
                lines.append(seg)
        else:
            head += f" · 实付 ¥{o['amount']:.2f}"
            lines.append(head)
        lines.append(f"【时间】付款 {_short(o['paid_at'])} → 发货 "
                     f"{_short(o['shipped_at']) or '未发货'} → 签收 {_short(o['received_at']) or '未签收'}")
        sla = []
        if o["ship_open"]:
            sla.append(f"付款后 {h(o['ship_hours'])} 仍未发货（承诺 {o['ship_sla']}h）")
        elif o["ship_hours"] is not None:
            sla.append(f"发货耗时 {h(o['ship_hours'])}"
                       + (f"，超出 {o['ship_sla']}h 时效" if o["ship_late"]
                          else f"，在 {o['ship_sla']}h 时效内"))
        if o["aftersale_wait_hours"] is not None:
            sla.append(f"售后等待 {h(o['aftersale_wait_hours'])}（时限 {o['aftersale_sla']}h）")
        if sla:
            lines.append("【时效】" + "；".join(sla))
        lg = o["logistics"]
        if o["shipped_at"]:
            seg = f"【物流】{lg['carrier'] or '未填承运商'} · 运单 {lg['tracking_no'] or '缺失'}"
            if lg["picked_at"]:
                seg += f" · 揽收 {_short(lg['picked_at'])}（发货后 {h(lg['pick_hours'])}）"
            elif lg["pick_wait_hours"] is not None:
                seg += f" · 发货后 {h(lg['pick_wait_hours'])} 仍无揽收记录（承诺 {o['pick_sla']}h 内）"
            lines.append(seg)
        if o["amount_mismatch"]:
            lines.append(f"【金额】商品小计 ¥{o['subtotal']:.2f} 与实付 ¥{o['amount']:.2f} 不一致，"
                         f"退款需扣减优惠/运费，不能直接按实付全退")
    else:
        lines.append("【订单】未关联订单，时效与金额都无法核验")

    if clause_rows:
        c0 = clause_rows[0]
        body = " ".join((c0["content"] or "").split())[:200]
        lines.append(f"【适用条款】《{c0['doc_name']}》{c0['clause_no']}：{body}")
        if t["points"]:
            lines.append(f"【量罚区间】该条对应扣分 {t['points']:g} 分起（同一行为整改期内重复发生按次累加）")
    else:
        lines.append("【适用条款】该类型在规则库中没有对应条款小节的原文，需人工核对")

    # 全量检索：把这一单的商品、类目、诉求描述带进条款库，补上硬映射覆盖不到的条文。
    related = _related_clauses(
        d, t, (o["item"]["title"] if o else ""), (o["category"] if o else ""))
    if related:
        if clause_rows:
            head = (f"按本案事实在 {_CLAUSE_CI['n']} 条现行条款中检索到 "
                    f"{len(related)} 条，供核对原文：")
        else:
            head = (f"该类型在规则库中无专项条款；以下 {len(related)} 条是按本案事实"
                    f"从 {_CLAUSE_CI['n']} 条里检索到的相近条文，仅作参考：")
        lines.append(f"【相关条款】{head}")
        for i, r in enumerate(related, 1):
            lines.append(f"  {i}. 《{r['doc']}》{r['clause']}：{r['content'][:90]}")
        lines.append("  （相关度只表示用词接近，不代表条款适用；引用哪一条由仲裁员判断）")

    # 「口径」这一段是这条功能的边界声明，不能省：
    # 没有它，运营会把上面的条款堆叠当成结论，直接复制进裁决书。
    if o and o["fake_ship"]:
        lines.append("【口径】本单已满足「已发货 + 未签收 + 超时无揽收」三项事实，"
                     "对应虚假发货的 4 分档；是否认定由仲裁员结合商家举证判断，本板块不下结论。")
    else:
        lines.append("【口径】以上为事实与条款原文，本板块不替仲裁员下结论。")
    lines.append("【下一步】裁决在「仲裁台」完成；需要商家答辩或补证的，在纠纷详情里发起。")
    return {"dispute_id": int(d["id"]), "dispute_no": d["dispute_no"],
            "dtype": d.get("dtype") or "", "text": "\n".join(lines),
            "related": related}


def _short(v) -> str:
    """把 '2026-10-24 14:20:00' 缩成 '10-24 14:20'：一屏里要塞六个时间点，
    带年份会把每一行都挤到折行。"""
    s = str(v or "")
    return s[5:16] if len(s) >= 16 else ""


def _history(shop_id: int, dtype: str) -> dict:
    """店铺历史：同类纠纷次数决定"初犯从轻、累犯加重"，
    这是规则 1.2「同一行为在整改期内重复发生的按次累加」在数据侧的落点。"""
    since = (dt.datetime.now() - dt.timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    same = common.fetch_all(
        "SELECT COUNT(*) AS n FROM t_dispute WHERE shop_id=%s AND dtype=%s "
        "AND created_at >= %s", (shop_id, dtype, since))[0]["n"] or 0
    lost = common.fetch_all(
        "SELECT COUNT(*) AS n FROM t_dispute WHERE shop_id=%s AND liability='merchant' "
        "AND created_at >= %s", (shop_id, since))[0]["n"] or 0
    return {"same_type": int(same), "lost_90d": int(lost)}


def analyze(d: dict) -> dict:
    """AI 调解员的完整判断：定责 + 方案 + 扣分 + 置信度 + 说理。

    纯函数风格（不写库），因为同一个纠纷会被反复评估：
    商家答辩后重评、仲裁员复审、AI 想重跑一次，都要能拿到一致结果。
    """
    t = DTYPES.get(d["dtype"]) or DTYPES["other"]
    ev = _keys(d)
    need = t["need"]
    miss = [x for x in need if x not in ev]
    amt = float(d.get("claim_amount") or 0)
    order = _order(d.get("order_no") or "")
    cap = float(order["amount"]) if order else amt
    hist = _history(int(d["shop_id"]), d["dtype"])
    steps: list[str] = []

    liability, remedy, pay, points, clause = "insufficient", "none", 0.0, 0.0, ""
    dtype = d["dtype"]

    if dtype == "quality":
        if "商品实拍" in ev:
            liability, remedy = "merchant", "return_refund"
            pay = min(amt or cap, cap) if cap else 0.0
            clause = "4.1"
            steps.append(f"买家提交了商品实拍与聊天记录，就「{d['title'][:40]}」完成了初步举证。")
            steps.append("依据《商家售后服务管理规范》4.1，质量争议由买家提供初步凭证、"
                         "商家主张商品完好的需自行举证；商家未提交有效反证。")
            steps.append(f"按同规范 3.1，售后申请逾期未处理视为商家同意，支持退货退款，"
                         f"退货运费由商家承担。")
        else:
            steps.append("买家未提供商品实拍，瑕疵是否存在无法通过材料判断，需人工核验。")

    elif dtype == "description":
        if {"商品实拍", "商品详情截图"} <= ev:
            liability, remedy = "merchant", "return_refund"
            pay = min(amt or cap, cap) if cap else 0.0
            clause = "2.2"
            points = 2.0
            steps.append("买家同时提供了商品实拍与商品详情截图，两者在材质/规格描述上存在明显不一致。")
            steps.append("依据《商家违规行为管理规则》2.2，商品描述与实际不符、夸大功效的，"
                         "每次扣 2 分；情节严重按严重违规处理。")
            steps.append("处理方案：支持退货退款，退回运费由商家承担。")
        else:
            steps.append("缺少商品实拍或详情页截图，无法比对描述与实物是否一致。")

    elif dtype == "shipping":
        if "订单截图" in ev or order:
            liability, remedy, clause = "merchant", "refund", "2.4"
            pay = min(amt or cap, cap) if cap else 0.0
            # 用订单真实时间算是否超 48 小时，不靠买家描述
            paid, shipped = _parse(order.get("paid_at") if order else None), \
                _parse(order.get("shipped_at") if order else None)
            late = bool(paid and shipped and (shipped - paid) > dt.timedelta(hours=48))
            # 虚假发货：有发货时间、没有有效揽收。判据来自订单的物流字段，
            # 不是买家说「物流一直不动」——那只是观感，运单号与揽收时间才是事实。
            od = order_detail(order["order_no"]) if order else None
            fake = bool(od and od["fake_ship"])
            if fake:
                points = 4.0
                if od["no_tracking"]:
                    steps.append(f"订单 {d.get('order_no')} 已标记发货，但未上传任何运单号，"
                                 f"承运商「{od['logistics'].get('carrier') or '未填写'}」无法核验。")
                else:
                    steps.append(f"订单 {d.get('order_no')} 运单号 "
                                 f"{od['logistics'].get('tracking_no')} 于发货后 "
                                 f"{od['logistics'].get('pick_wait_hours')} 小时仍无揽收记录"
                                 f"（时限 {PICK_SLA_HOURS} 小时）。")
                steps.append("依据《商家违规行为管理规则》2.4，上传无效运单号、"
                             "以虚假物流信息规避发货义务的，按虚假发货每次扣 4 分。")
            else:
                points = 0.5
                if late:
                    steps.append(f"订单 {d.get('order_no')} 付款后 "
                                 f"{round((shipped - paid).total_seconds() / 3600, 1)} 小时才出库，"
                                 f"超过承诺的 48 小时时效。")
                else:
                    steps.append(f"订单 {d.get('order_no') or '（未提供单号）'} 已按承诺时效出库，"
                                 f"物流停滞属承运方责任，平台支持买家退款并向物流追偿。")
                steps.append("依据《商家违规行为管理规则》2.4，未在承诺时效内发货的每单扣 0.5 分；"
                             "上传无效运单号的按虚假发货每次扣 4 分。")
            steps.append("处理方案：支持仅退款，款项原路退回。")
        else:
            steps.append("买家未提供订单信息，无法核验发货时效。")

    elif dtype == "aftersale":
        if {"售后申请截图", "聊天记录"} <= ev:
            liability, remedy, clause, points = "merchant", "return_refund", "2.3", 1.0
            pay = min(amt or cap, cap) if cap else 0.0
            steps.append("买家提交了售后申请记录与沟通记录，商家在 36 小时内未完成审核处理。")
            steps.append("依据《商家售后服务管理规范》2.3.3 与 3.1，商家逾期未处理的，"
                         "视为同意消费者的售后申请，系统自动执行退款。")
            steps.append("依据《商家违规行为管理规则》2.3，售后超时未处理每次扣 1 分，"
                         "并纳入服务体验分考核。")
        else:
            steps.append("缺少售后申请截图或沟通记录，无法确认商家是否逾期未处理。")

    elif dtype == "counterfeit":
        if {"商品实拍", "检测报告"} <= ev:
            liability, remedy, clause, points = "merchant", "refund_compensate", "3.1", 24.0
            pay = min(amt or cap, cap) if cap else 0.0
            steps.append("买家提供了商品实拍与鉴定/检测报告，初步证明所售商品为假冒商品。")
            steps.append("依据《商家违规行为管理规则》3.1，售假每次扣 24 分，商品全部下架，"
                         "扣除全部保证金作为违约金。")
            steps.append("该结论涉及店铺重大处置，必须由平台仲裁员复核后生效。")
        else:
            steps.append("涉假主张需要检测报告或品牌方鉴定，当前材料不足，转人工核验。")

    elif dtype == "fraud":
        if "聊天记录" in ev:
            liability, remedy, clause, points = "merchant", "refund", "3.2", 48.0
            pay = min(amt or cap, cap) if cap else 0.0
            steps.append("聊天记录显示商家引导买家脱离平台交易，属《商家违规行为管理规则》3.2 情形。")
            steps.append("依据 3.2，以非法占有为目的诱导站外交易的，每次扣 48 分，店铺清退。")
            steps.append("该结论涉及店铺清退，必须由平台仲裁员复核后生效。")
        else:
            steps.append("缺少聊天记录，无法判断是否存在站外交易引导。")

    elif dtype == "service":
        if "聊天记录" in ev:
            liability, remedy = "merchant", "apology"
            steps.append("聊天记录显示客服存在推诿、长时间不回复的情形。")
            steps.append("平台规则未对服务态度设定扣分标准，"
                         "因此本次不扣分，仅要求商家书面整改并计入服务体验分。")
        else:
            steps.append("缺少聊天记录，无法核实服务过程。")

    elif dtype == "safety":
        if {"检测报告", "医疗凭证"} <= ev:
            liability, remedy, clause, points = "merchant", "refund_compensate", "3.3", 48.0
            pay = min(amt or cap, cap) if cap else 0.0
            steps.append("买家提供了检测报告与医疗凭证，商品存在危害人身安全的缺陷。")
            steps.append("依据《商家违规行为管理规则》3.3，每次扣 48 分，店铺清退并移交监管部门。")
        else:
            steps.append("危及人身安全的主张需要检测报告与医疗凭证，当前材料不足，转人工核验。")

    else:
        steps.append("纠纷类型为其他，无对应规则条款，转人工判断。")

    # ---- 置信度：把"我有多确定"拆成可解释的加减项，而不是拍一个数
    conf = 0.60
    reasons = []
    if not miss:
        conf += 0.14
        reasons.append("+0.14 举证材料齐全")
    elif len(miss) == 1:
        conf += 0.04
        reasons.append("+0.04 缺 1 项材料")
    else:
        conf -= 0.10
        reasons.append("-0.10 材料缺失")
    if amt <= 200:
        conf += 0.08
        reasons.append("+0.08 低金额")
    elif amt > 500:
        conf += 0.03
    if amt > AUTO_MAX_AMOUNT:
        conf -= 0.15
        reasons.append("-0.15 金额超自动裁决上限")
    if hist["same_type"] <= 1:
        conf += 0.06
        reasons.append("+0.06 同类纠纷少")
    elif hist["same_type"] >= 3:
        conf -= 0.05
        reasons.append("-0.05 同类纠纷频发")
    if d.get("order_no"):
        conf += 0.04
    if d.get("session_id"):
        conf += 0.03
    if shop_status(int(d["shop_id"])) in ("suspended", "closed"):
        conf -= 0.10
        reasons.append("-0.10 店铺已在处罚中")
    if t["severity"] == "critical":
        conf -= 0.30
        reasons.append("-0.30 严重违规必须人工")
    if liability == "insufficient":
        conf = min(conf, 0.45)
        reasons.append("→ 证据不足，置信度封顶 0.45")
    conf = round(max(0.0, min(1.0, conf)), 4)

    # ---- 是否自动裁决
    auto_ok = (bool(t["auto"]) and liability == "merchant" and conf >= AUTO_CONF
               and amt <= AUTO_MAX_AMOUNT and t["severity"] != "critical")
    if auto_ok:
        gate = "auto"
    elif t["severity"] == "critical":
        gate = "critical"
    elif amt > AUTO_MAX_AMOUNT:
        gate = "amount"
    elif liability == "insufficient":
        gate = "evidence"
    else:
        gate = "low_conf"

    return {
        "liability": liability, "remedy": remedy, "amount": round(pay, 2),
        "points": points, "clause": clause, "doc": t.get("doc") or "",
        "confidence": conf, "auto_ok": auto_ok, "gate": gate,
        "missing": miss, "history": hist,
        "steps": steps, "conf_steps": reasons,
        "label": t["label"], "severity": t["severity"],
    }


def _basis_text(a: dict) -> str:
    if not a["clause"]:
        return ""
    return f"《{a['doc']}》{a['clause']}"


def _template_reasoning(d: dict, a: dict) -> str:
    head = (f"平台受理编号 {d['dispute_no']} 的纠纷（{a['label']}）。")
    body = "".join(a["steps"])
    if a["liability"] == "merchant":
        tail = (f"综上，判定商家承担责任，处理方案为{REMEDY_LABEL.get(a['remedy'], a['remedy'])}"
                + (f"，金额 {a['amount']:.2f} 元" if a["amount"] else "")
                + (f"，并对商家扣 {a['points']:g} 分" if a["points"] else "，本次不扣分")
                + "。商家可在 7 天内对本裁决提起申诉。")
    elif a["liability"] == "insufficient":
        tail = "综上，现有材料不足以认定责任，本单转平台仲裁员人工处理，请双方补充材料。"
    else:
        tail = "综上，本单证据不足或不属于商家责任范围，转人工复核。"
    return head + body + tail


POLISH_SYSTEM = (
    "你是电商平台的纠纷调解员，正在撰写一份给买卖双方看的裁决说明。\n"
    "硬性规则：\n"
    "1. 只能引用【可引用条款】中列出的条款号，不许新增任何条款号，不许编造条款原文。\n"
    "2. 不许承诺【处理方案】以外的赔偿，不许新增金额、时效、补偿。\n"
    "3. 事实部分只能使用【事实要点】里给出的内容，不许补充未提及的情节。\n"
    "4. 120 字以内，先事实、再依据、最后结论，语气中立，不用 markdown、不用列表。\n"
    "直接输出正文。"
)


def polish(d: dict, a: dict) -> tuple[str, dict]:
    """让模型把说理写顺，但不许它改结论、不许它加条款。

    引用校验：模型输出里出现的每个条款号都必须在依据集合里，否则整段丢弃。
    这和 v5 问答的引用闸门是同一条纪律——**模型可以说得不好，但不能说得不准**。
    """
    llm = get_llm()
    tpl = _template_reasoning(d, a)
    if not llm.available():
        return tpl, {"generator": "template", "reason": "no_llm"}

    basis = [x for x in [_basis_text(a)] if x]
    user = (f"【可引用条款】{('、'.join(basis) or '（无）')}\n"
            f"【处理方案】{REMEDY_LABEL.get(a['remedy'], a['remedy'])}，"
            f"金额 {a['amount']:.2f} 元，扣分 {a['points']:g} 分\n"
            f"【事实要点】{''.join(a['steps'])}\n"
            f"【纠纷标题】{d['title']}\n"
            f"【买家诉求】{(d.get('claim') or '')[:200]}\n\n请撰写裁决说明。")
    try:
        out, meta = llm.chat(
            [{"role": "system", "content": POLISH_SYSTEM},
             {"role": "user", "content": user}],
            temperature=0.2, max_tokens=320)
    except Exception as e:
        return tpl, {"generator": "template", "reason": f"call_failed:{str(e)[:60]}"}
    if not out:
        return tpl, {**meta, "generator": "template", "reason": "empty"}

    text = out.strip().strip('"').strip()
    cited = set(re.findall(r"第[一二三四五六七八九十百零\d]+条|\d+\.\d+(?:\.\d+)?", text))
    allowed = set(re.findall(r"第[一二三四五六七八九十百零\d]+条|\d+\.\d+(?:\.\d+)?",
                             " ".join(basis)))
    bad = cited - allowed
    if bad:
        return tpl, {"generator": "template", "reason": "citation_check_failed",
                     "invented": sorted(bad)[:5], **meta}
    if len(text) < 20:
        return tpl, {**meta, "generator": "template", "reason": "too_short"}
    return text[:600], {**meta, "generator": "llm"}


# ---------------------------------------------------------------- 纠纷主流程

def create(shop_id: int, dtype: str, title: str, claim: str, claim_amount: float,
           evidence: list[dict], buyer: dict, session_id: str | None = None,
           order_no: str = "", claim_type: str = "refund") -> dict:
    if dtype not in DTYPES:
        raise ValueError("未知的纠纷类型")
    if not (title or "").strip():
        raise ValueError("请填写纠纷标题")
    if not shop_id:
        raise ValueError("请选择被投诉的店铺")
    if shop_id == buyer.get("shop_id"):
        raise ValueError("不能投诉自己经营的店铺")

    st = shop_status(shop_id)
    if st == "closed":
        raise ValueError("该店铺已被平台清退，无法发起新的纠纷")

    # 一个会话只能挂一张未关闭的纠纷单。会话页和投诉表单都能带 session_id 进来，
    # 没有这道闸，同一个人点两次就是两单，平台要判两遍，商家被扣两次分。
    if session_id:
        rows = common.fetch_all(
            "SELECT dispute_no FROM t_dispute WHERE session_id=%s "
            "AND status NOT IN ('closed') ORDER BY id DESC LIMIT 1", (session_id,))
        if rows:
            raise ValueError(f"该会话已提交过平台介入（{rows[0]['dispute_no']}），请勿重复提交")

    a = analyze({"dtype": dtype, "title": title, "claim": claim,
                 "claim_amount": claim_amount, "evidence": json.dumps(evidence or []),
                 "shop_id": shop_id, "order_no": order_no, "session_id": session_id})

    status = "resolved" if a["auto_ok"] else "pending_human"
    priority = 9 if a["severity"] == "critical" else (7 if a["amount"] > 500 else 5)
    ts = _now()

    did = 0
    for attempt in range(3):
        try:
            did = common.insert(
                """INSERT INTO t_dispute (dispute_no, session_id, order_no, shop_id, buyer_id,
                       buyer_name, dtype, title, claim, claim_type, claim_amount, evidence,
                       status, stage, liability, ai_confidence, ai_remedy, ai_amount,
                       ai_penalty, ai_points, ai_reasoning, ai_basis, priority, sla_due,
                       created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (next_no(), session_id, (order_no or "")[:40], shop_id, buyer["id"],
                 (buyer.get("display_name") or "买家")[:60], dtype, title[:200],
                 (claim or "")[:2000], claim_type, claim_amount,
                 json.dumps(evidence or [], ensure_ascii=False),
                 status, "ai" if status == "resolved" else "human", a["liability"],
                 a["confidence"], a["remedy"], a["amount"], "points" if a["points"] else "",
                 a["points"], "",
                 _basis_text(a)[:250], priority,
                 _plus(hours=SLA_HOURS) if status == "pending_human" else None, ts, ts))
            break
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(0.05)

    d = get(did)
    add_msg(did, "buyer", f"【投诉】{title}\n\n{claim or ''}\n\n诉求金额：{claim_amount:.2f} 元",
            "text")
    for e in evidence or []:
        add_msg(did, "buyer", f"凭证：{e.get('k')} — {e.get('v') or '（已上传）'}", "evidence")

    # AI 说理（模板兜底，模型润色）+ 落库
    text, meta = polish(d, a)
    common.execute("UPDATE t_dispute SET ai_reasoning=%s, ai_basis=%s WHERE id=%s",
                   (text, _basis_text(a)[:250], did))
    add_msg(did, "ai", text, "reasoning", json.dumps(
        {"confidence": a["confidence"], "gate": a["gate"],
         "missing": a["missing"], "generator": meta.get("generator")}, ensure_ascii=False))

    # 累计店铺纠纷数
    common.execute("UPDATE t_shop_credit SET dispute_count=dispute_count+1, updated_at=%s "
                   "WHERE shop_id=%s", (ts, shop_id))

    out = {"id": did, "dispute_no": d["dispute_no"], "status": status,
           "liability": a["liability"], "confidence": a["confidence"],
           "remedy": a["remedy"], "amount": a["amount"], "points": a["points"],
           "basis": _basis_text(a), "reasoning": text, "gate": a["gate"],
           "missing": a["missing"], "generator": meta.get("generator")}

    if status == "resolved":
        out["penalty"] = _settle(d, a, auto=True, operator=None, note=text)
    else:
        add_msg(did, "system",
                f"置信度 {a['confidence']:.2f} 未达自动裁决线（{AUTO_CONF:.2f}），"
                f"已转平台仲裁员处理，{SLA_HOURS} 小时内响应", "system")
    return out


def _settle(d: dict, a: dict, auto: bool, operator: int | None,
            note: str = "") -> dict:
    """结案：落裁决 + 生成处罚 + 更新店铺信用。"""
    ts = _now()
    pen = {"penalty_id": None}
    if a["liability"] == "merchant" and a["points"] > 0:
        pen = punish(int(d["shop_id"]), int(d["id"]), a["points"], a["clause"],
                     f"{d['dispute_no']} {a['label']}", auto, operator,
                     basis=_basis_text(a))
    elif a["liability"] == "merchant":
        # 不扣分也要留痕：否则"这家店被认定有责几次"查不出来，
        # 但这属于责任认定记录（record），不是处罚，不影响店铺状态。
        pen = {"penalty_id": None, "points": 0}
        common.execute(
            "INSERT INTO t_penalty (shop_id, dispute_id, ptype, points, node, reason, "
            "basis, clause_no, status, auto, operator_id, effective_at, created_at) "
            "VALUES (%s,%s,'record',0,'',%s,%s,%s,'active',%s,%s,%s,%s)",
            (d["shop_id"], d["id"], f"{d['dispute_no']} {a['label']}（认定有责，不扣分）",
             _basis_text(a), a["clause"] or "", 1 if auto else 0, operator, ts, ts))

    common.execute(
        "UPDATE t_dispute SET status='resolved', stage=%s, liability=%s, ai_remedy=%s, "
        "ai_amount=%s, ai_points=%s, verdict=%s, verdict_by=%s, verdict_at=%s, "
        "penalty_id=%s, updated_at=%s, closed_at=%s WHERE id=%s",
        ("ai" if auto else "human", a["liability"], a["remedy"], a["amount"],
         a["points"], (note or a.get("reasoning") or "")[:2000], operator, ts,
         pen.get("penalty_id"), ts, ts, d["id"]))
    if a["liability"] == "merchant":
        common.execute("UPDATE t_shop_credit SET dispute_lost=dispute_lost+1, updated_at=%s "
                       "WHERE shop_id=%s", (ts, d["shop_id"]))
    vtext = (("AI 自动裁决生效：" if auto else "仲裁员裁决：")
             + f"{'商家责任' if a['liability'] == 'merchant' else '证据不足/非商家责任'}，"
             + f"处理方案 {REMEDY_LABEL.get(a['remedy'], a['remedy'])}"
             + (f"，扣 {a['points']:g} 分" if a["points"] else "，不扣分"))
    add_msg(int(d["id"]), "system", vtext, "verdict")
    # 裁决结果要回到买家当初咨询的那个窗口，否则他在会话里永远停在"已转平台"
    _notify_cs(d, f"平台裁决：{vtext}\n完整裁决理由请在「纠纷中心」查看")
    return pen


def claim(dispute_id: int, user: dict) -> dict:
    d = get(dispute_id)
    if not d:
        raise ValueError("纠纷不存在")
    if d["status"] != "pending_human":
        raise ValueError("该纠纷当前不在待仲裁状态")
    common.execute("UPDATE t_dispute SET status='arbitrating', stage='human', "
                   "verdict_by=%s, updated_at=%s WHERE id=%s",
                   (user["id"], _now(), dispute_id))
    add_msg(dispute_id, "system", f"平台仲裁员 {user['display_name']} 已受理", "system")
    return {"ok": True}


def verdict(dispute_id: int, user: dict, liability: str, remedy: str,
            amount: float, points: float, clause: str, note: str,
            ptype: str = "", days: int | None = None) -> dict:
    """人工裁决。仲裁员可以改 AI 的结论，但必须写理由——这是可审计的底线。"""
    d = get(dispute_id)
    if not d:
        raise ValueError("纠纷不存在")
    if d["status"] not in ("pending_human", "arbitrating"):
        raise ValueError("该纠纷已结案，如需变更请走申诉流程")
    if not (note or "").strip():
        raise ValueError("请填写裁决理由，申诉复核时需要据此说明")

    a = analyze(d)
    a.update({"liability": liability, "remedy": remedy,
              "amount": max(0.0, float(amount or 0)),
              # 单次违规扣分上限按规则库里最重的条款（3.3 危及人身安全 48 分）封顶，
              # 防止手滑多打一个 0 把店直接扣死。
              "points": min(48.0, max(0.0, float(points or 0))),
              "clause": (clause or "").strip(),
              "reasoning": note})
    a["doc"] = a.get("doc") or DTYPES.get(d["dtype"], {}).get("doc", "")
    if a["clause"]:
        a["doc"] = verify_clause(a["clause"]) or a["doc"]

    add_msg(dispute_id, "arbiter", note, "verdict")
    pen = _settle(d, a, auto=False, operator=user["id"], note=note)
    extra = {}
    if ptype:
        extra = manual_penalty(int(d["shop_id"]), ptype, days,
                               f"{d['dispute_no']} 仲裁裁量：{note[:80]}",
                               user["id"], dispute_id)
    return {"ok": True, "penalty": pen, "extra": extra}


def verify_clause(clause_no: str) -> str:
    """核对条款号是否真实存在。仲裁员手填的条款号也过一次校验——
    人比模型可靠，但不是不出错，而处罚书上写错条款是硬伤。"""
    rows = common.fetch_all(
        "SELECT d.doc_name FROM t_clause c JOIN t_rule_doc d ON d.id=c.doc_id "
        "WHERE c.clause_no=%s LIMIT 1", (clause_no.strip(),))
    return rows[0]["doc_name"] if rows else ""


def reply(dispute_id: int, user: dict, text: str, as_role: str) -> dict:
    """双方补充陈述。商家在上面答辩，买家在上面补材料。

    商家在自动裁决后 48 小时内答辩的，重跑一次评估——AI 判错不该只能靠申诉纠正，
    答辩阶段就该有一次纠错机会。改判则自动撤销处罚。
    """
    d = get(dispute_id)
    if not d:
        raise ValueError("纠纷不存在")
    if d["status"] in ("closed", "overturned"):
        raise ValueError("该纠纷已关闭")
    t = (text or "").strip()
    if not t:
        raise ValueError("内容不能为空")
    add_msg(dispute_id, as_role, t[:2000], "text")

    out: dict = {"ok": True}
    if as_role == "merchant" and d["status"] == "resolved" and d["stage"] == "ai":
        created = _parse(d.get("created_at"))
        if created and (dt.datetime.now() - created) <= dt.timedelta(hours=48):
            a = analyze({**d, "evidence": d.get("evidence")})
            a["steps"] = a["steps"] + ["商家在答辩期内提交了说明，平台据此重新核验。"]
            new_text, _ = polish(d, a)
            common.execute("UPDATE t_dispute SET ai_reasoning=%s, ai_confidence=%s "
                           "WHERE id=%s", (new_text, a["confidence"], dispute_id))
            add_msg(dispute_id, "ai", new_text, "reasoning")
            if a["liability"] != "merchant":
                if d.get("penalty_id"):
                    revoke_penalty(int(d["penalty_id"]), None, "商家答辩后改判")
                common.execute(
                    "UPDATE t_dispute SET status='resolved', liability=%s, ai_points=0, "
                    "ai_remedy='reject', verdict=%s, closed_at=%s, updated_at=%s WHERE id=%s",
                    (a["liability"], new_text[:2000], _now(), _now(), dispute_id))
                add_msg(dispute_id, "system", "商家答辩成立，原裁决已改判，相关处罚已撤销", "verdict")
                out["overturned"] = True
            elif a["confidence"] < AUTO_CONF:
                common.execute(
                    "UPDATE t_dispute SET status='pending_human', stage='human', "
                    "sla_due=%s, updated_at=%s WHERE id=%s",
                    (_plus(hours=SLA_HOURS), _now(), dispute_id))
                add_msg(dispute_id, "system", "商家答辩后置信度下降，已转人工复核", "system")
                out["to_human"] = True
    return out


def close(dispute_id: int, user: dict, reason: str = "买家撤回投诉") -> dict:
    d = get(dispute_id)
    if not d:
        raise ValueError("纠纷不存在")
    if d["status"] in ("resolved", "overturned", "closed"):
        raise ValueError("该纠纷已结案")
    common.execute("UPDATE t_dispute SET status='closed', verdict=%s, closed_at=%s, "
                   "updated_at=%s WHERE id=%s", (reason[:500], _now(), _now(), dispute_id))
    add_msg(dispute_id, "system", f"纠纷已关闭：{reason}", "system")
    _notify_cs(d, f"纠纷已关闭：{reason}")
    return {"ok": True}


# ---------------------------------------------------------------- 申诉

def appeal_create(target_type: str, target_id: int, user: dict, reason: str,
                  evidence: list[dict]) -> dict:
    """申诉入口。程序性审查（超期/重复/空理由）当场拒绝，
    实体审查（该不该撤销）一律进人工队列——AI 不替平台做最终决定。"""
    if target_type not in ("penalty", "verdict"):
        raise ValueError("未知的申诉对象")
    if not (reason or "").strip() or len(reason.strip()) < 10:
        raise ValueError("请至少用 10 个字说明申诉理由，否则无法受理")

    if target_type == "penalty":
        rows = common.fetch_all("SELECT * FROM t_penalty WHERE id=%s", (target_id,))
        if not rows:
            raise ValueError("处罚单不存在")
        p = rows[0]
        shop_id, did = int(p["shop_id"]), p.get("dispute_id")
        if p["status"] != "active":
            raise ValueError("该处罚单已被撤销或已失效")
        since = _parse(p.get("effective_at") or p.get("created_at"))
        if user["role"] == "merchant" and user.get("shop_id") != shop_id:
            raise ValueError("只能申诉本店铺的处罚")
    else:
        d = get(target_id)
        if not d:
            raise ValueError("纠纷不存在")
        if d["status"] != "resolved":
            raise ValueError("只有已裁决的纠纷可以申诉")
        shop_id, did = int(d["shop_id"]), target_id
        if user["role"] == "merchant" and user.get("shop_id") != shop_id:
            if d.get("buyer_id") != user["id"]:
                raise ValueError("只能申诉与自己相关的纠纷")
        since = _parse(d.get("verdict_at") or d.get("closed_at"))

    if since and (dt.datetime.now() - since) > dt.timedelta(days=APPEAL_DAYS):
        raise ValueError(f"已超过 {APPEAL_DAYS} 天申诉期，平台不再受理")

    # 重复申诉：pending/human_review/accepted 状态下视为「已在处理」，直接拦；
    # 但 rejected（已驳回）允许走「补充材料 → 复议」，而不是硬堵死。
    # 这里只拦「还没处理完」的重复，rejected 的复议走 appeal_reopen。
    dup = common.fetch_all(
        "SELECT id, status FROM t_appeal WHERE target_type=%s AND target_id=%s "
        "AND appellant_id=%s ORDER BY id DESC LIMIT 1",
        (target_type, target_id, user["id"]))
    if dup and dup[0]["status"] in ("pending", "human_review", "accepted"):
        raise ValueError("同一事项已有申诉在处理中，请勿重复提交")

    aid = common.insert(
        """INSERT INTO t_appeal (target_type, target_id, shop_id, dispute_id, appellant_id,
               appellant_role, reason, evidence, status, created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)""",
        (target_type, target_id, shop_id, did, user["id"],
         "merchant" if user["role"] == "merchant" else "buyer",
         reason.strip()[:2000],
         json.dumps(evidence or [], ensure_ascii=False), _now(), _now()))
    common.execute("UPDATE t_shop_credit SET appeal_count=appeal_count+1 WHERE shop_id=%s",
                   (shop_id,))
    out = screen(aid)
    return {"ok": True, "id": aid, **out}


def screen(appeal_id: int) -> dict:
    """AI 初审：只判断"这单有没有复核价值"，不给结论。

    判断依据是可核对的四件事，而不是"我觉得原判对不对"：
      ① 是否提出新证据  ② 是否指出原判证据链缺口
      ③ 原判是否为 AI 自动裁决（自动裁决本就该更容许复核）
      ④ 原判处罚是否已接近节点（错判代价大）
    """
    rows = common.fetch_all("SELECT * FROM t_appeal WHERE id=%s", (appeal_id,))
    if not rows:
        raise ValueError("申诉单不存在")
    ap = rows[0]
    ev = _json(ap.get("evidence"), [])
    reason = ap.get("reason") or ""
    checks: list[str] = []
    score = 0.0

    if ev:
        score += 0.25
        checks.append(f"申诉人提交了 {len(ev)} 项新证据，具备复核基础")
    else:
        checks.append("未提交新证据，仅对结论有异议")

    if re.search(r"条款|依据|适用|规则|\d+\.\d+", reason):
        score += 0.15
        checks.append("申诉理由指向条款适用，属于应当复核的情形")

    if ap["target_type"] == "penalty":
        p = common.fetch_all("SELECT * FROM t_penalty WHERE id=%s", (ap["target_id"],))
        p = p[0] if p else None
        if p and p.get("auto"):
            score += 0.10
            checks.append("原处罚由 AI 自动裁决生成，按平台惯例应容许一次人工复核")
        if p and p.get("ptype") in ("close", "suspend"):
            score += 0.20
            checks.append("原处罚涉及清退/停业，属重大处置，必须人工复核")
        if p and float(p.get("points") or 0) >= 24:
            score += 0.15
            checks.append("原处罚扣分达到 24 分以上，影响面较大")
    else:
        d = get(int(ap["target_id"]))
        if d and d.get("stage") == "ai":
            score += 0.10
            checks.append("原裁决为 AI 自动裁决，应容许人工复核")
        if d and not evidence_of(d):
            score += 0.15
            checks.append("原裁决的证据材料不完整")

    conf = round(min(1.0, score), 4)
    suggest = "accept" if conf >= 0.40 else "reject"
    view = ("；".join(checks) + "。"
            + ("建议受理并转人工复核。" if suggest == "accept"
               else "建议驳回：申诉未提出可复核的新事实，原裁决依据充分。"))
    common.execute("UPDATE t_appeal SET status='human_review', ai_view=%s, ai_suggest=%s, "
                   "ai_confidence=%s, review_deadline=%s, updated_at=%s WHERE id=%s",
                   (view, suggest, conf, _plus(hours=APPEAL_REVIEW_HOURS), _now(), appeal_id))
    return {"ai_view": view, "ai_suggest": suggest, "ai_confidence": conf}


def appeal_decide(appeal_id: int, user: dict, accept: bool, result: str) -> dict:
    rows = common.fetch_all("SELECT * FROM t_appeal WHERE id=%s", (appeal_id,))
    if not rows:
        raise ValueError("申诉单不存在")
    ap = rows[0]
    if ap["status"] not in ("pending", "human_review"):
        raise ValueError("该申诉已处理")
    if not (result or "").strip():
        raise ValueError("请填写复核意见")

    out = {"ok": True, "accepted": accept}
    note = ""
    if accept:
        if ap["target_type"] == "penalty":
            out["revoke"] = revoke_penalty(int(ap["target_id"]), user["id"],
                                           f"申诉成立：{result[:120]}")
            note = f"平台申诉复核：申诉成立，相关处罚已撤销。{result[:120]}"
        else:
            d = get(int(ap["target_id"]))
            if d and d.get("penalty_id"):
                out["revoke"] = revoke_penalty(int(d["penalty_id"]), user["id"],
                                               f"裁决申诉成立：{result[:120]}")
            common.execute(
                "UPDATE t_dispute SET status='overturned', verdict=%s, updated_at=%s "
                "WHERE id=%s",
                (f"【申诉改判】{result[:900]}", _now(), int(ap["target_id"])))
            add_msg(int(ap["target_id"]), "system", f"申诉成立，原裁决被撤销：{result[:300]}",
                    "verdict")
            note = f"平台申诉复核：申诉成立，原裁决被撤销。{result[:120]}"
    else:
        if ap.get("dispute_id"):
            add_msg(int(ap["dispute_id"]), "system",
                    f"申诉被驳回：{result[:300]}", "system")
            note = f"平台申诉复核：申诉被驳回。{result[:120]}"
    # 申诉结果同样要回到会话：买家/商家可能只在咨询窗口里等结果
    if note and ap.get("dispute_id"):
        _notify_dispute(int(ap["dispute_id"]), note)

    common.execute(
        "UPDATE t_appeal SET status=%s, result=%s, reviewer_id=%s, decided_at=%s, "
        "updated_at=%s WHERE id=%s",
        ("accepted" if accept else "rejected", result[:2000], user["id"],
         _now(), _now(), appeal_id))
    return out


def appeal_supplement(appeal_id: int, user: dict, evidence: list[dict],
                      note: str) -> dict:
    """补充材料：申诉人可在「待审 / 人工复核中」阶段追加证据，追加后重新做 AI 初审。

    为什么补充后要重跑 screen 而不是直接塞进去：
      初审判断「这单有没有复核价值」的依据就是证据，补了新证据后原来的
      ai_suggest 可能已经过时（比如原来没证据建议驳回，补了开箱视频就该受理）。
      重跑 screen 让 AI 初审结论始终对应当前证据，而不是停在提交那一刻。
    """
    rows = common.fetch_all("SELECT * FROM t_appeal WHERE id=%s", (appeal_id,))
    if not rows:
        raise ValueError("申诉单不存在")
    ap = rows[0]
    if ap["appellant_id"] != user["id"]:
        raise ValueError("只能补充自己提交的申诉")
    if ap["status"] not in ("pending", "human_review"):
        raise ValueError("当前状态不允许补充材料（已处理的申诉走复议）")
    if (ap.get("supplement_count") or 0) >= APPEAL_MAX_SUPPLEMENT:
        raise ValueError(f"最多补充 {APPEAL_MAX_SUPPLEMENT} 次材料")

    # 合并新旧证据：补充是「追加」不是「覆盖」，旧证据仍参与初审。
    merged = _json(ap.get("evidence"), []) + (evidence or [])
    hist = _json(ap.get("supplement_history"), [])
    hist.append({"at": _now(), "note": (note or "")[:300],
                 "added": len(evidence or [])})
    common.execute(
        "UPDATE t_appeal SET evidence=%s, supplement_count=supplement_count+1, "
        "supplement_history=%s, status='pending', updated_at=%s WHERE id=%s",
        (json.dumps(merged, ensure_ascii=False), json.dumps(hist, ensure_ascii=False),
         _now(), appeal_id))
    # 补了证据，重新初审
    out = screen(appeal_id)
    return {"ok": True, "id": appeal_id, "supplement_count": (ap.get("supplement_count") or 0) + 1,
            **out}


def appeal_reopen(appeal_id: int, user: dict, evidence: list[dict],
                  note: str) -> dict:
    """二次复核入口：被驳回的申诉，补充新证据后申请复议一次。

    和 appeal_supplement 的区别：supplement 是「处理中」追加材料，reopen 是
    「已驳回」重新打开。驳回不是终点——申诉人有权在补充了原判没看到的新证据后，
    要求平台再看一次。但只允许一次，避免「驳回→复议→再驳回→再复议」的死循环。
    """
    rows = common.fetch_all("SELECT * FROM t_appeal WHERE id=%s", (appeal_id,))
    if not rows:
        raise ValueError("申诉单不存在")
    ap = rows[0]
    if ap["appellant_id"] != user["id"]:
        raise ValueError("只能复议自己提交的申诉")
    if ap["status"] != "rejected":
        raise ValueError("只有被驳回的申诉可以申请复议")
    if (ap.get("supplement_count") or 0) >= APPEAL_MAX_SUPPLEMENT:
        raise ValueError("复议机会已用完，同一事项不再受理")

    merged = _json(ap.get("evidence"), []) + (evidence or [])
    hist = _json(ap.get("supplement_history"), [])
    hist.append({"at": _now(), "note": "复议：" + (note or "")[:290],
                 "added": len(evidence or [])})
    common.execute(
        "UPDATE t_appeal SET evidence=%s, supplement_count=supplement_count+1, "
        "supplement_history=%s, status='pending', result=NULL, decided_at=NULL, "
        "updated_at=%s WHERE id=%s",
        (json.dumps(merged, ensure_ascii=False), json.dumps(hist, ensure_ascii=False),
         _now(), appeal_id))
    out = screen(appeal_id)
    return {"ok": True, "id": appeal_id, "reopened": True, **out}


def appeal_overdue_hours(appeal: dict) -> float | None:
    """人工复核是否超时：返回超时时长（小时），未超时/无时限返回 None。

    不做定时任务，靠读取时惰性计算——和封禁到期自愈同一套思路：
    谁来看申诉列表，谁就能看到「这条已经挂了多久没处理」。
    """
    dl = _parse(appeal.get("review_deadline"))
    if not dl or appeal.get("status") != "human_review":
        return None
    gap = (dt.datetime.now() - dl).total_seconds() / 3600
    return round(gap, 1) if gap > 0 else None


# ---------------------------------------------------------------- 指标

def board_stats() -> dict:
    """治理板块顶部的"三方接入"计数。

    运营进咨询窗口，第一眼要知道的不是"知识库有多少条"，而是"有多少事在等我"。
    这三个数字分别对应三方的动作：买家提交后等平台受理、平台受理后等商家答辩、
    商家/买家都在等裁决。少一个，运营就得切到另一个页面去数。
    """
    pend = common.fetch_all(
        "SELECT COUNT(*) AS n FROM t_dispute WHERE status IN ('pending_human','arbitrating')"
    )[0]["n"] or 0
    # 待答辩 = 还在平台手里、且商家一句话都还没说。用 t_dispute_msg 里有没有
    # merchant 角色的消息判断，比再建一个状态位可靠——状态位会漏改。
    no_reply = common.fetch_all(
        """SELECT COUNT(*) AS n FROM t_dispute d
           WHERE d.status IN ('pending_human','arbitrating')
             AND NOT EXISTS (SELECT 1 FROM t_dispute_msg m
                             WHERE m.dispute_id=d.id AND m.role='merchant')""")[0]["n"] or 0
    shops = {r["status"]: r["n"] for r in common.fetch_all(
        "SELECT status, COUNT(*) AS n FROM t_shop_credit GROUP BY status")}
    restrict = sum(shops.get(k, 0) for k in ("suspended", "closed", "restricted", "downgraded"))
    appeals = common.fetch_all(
        "SELECT COUNT(*) AS n FROM t_appeal WHERE status='pending'")[0]["n"] or 0
    return {
        "pending_arbitrate": pend,
        "pending_defense": no_reply,
        "restricted_shops": restrict,
        "pending_appeals": appeals,
        "shops": shops,
    }


def metrics() -> dict:
    # 看板上的「风险店铺」是运营对外的口径，进来先把到期罚单结算掉，
    # 否则会看到一家 7 天前就该恢复营业的店还挂在「暂停营业」里。
    try:
        expire_due()
    except Exception:
        pass
    total = common.fetch_all("SELECT COUNT(*) AS n FROM t_dispute")[0]["n"] or 0
    by_status = {r["status"]: r["n"] for r in common.fetch_all(
        "SELECT status, COUNT(*) AS n FROM t_dispute GROUP BY status")}
    by_type = {r["dtype"]: r["n"] for r in common.fetch_all(
        "SELECT dtype, COUNT(*) AS n FROM t_dispute GROUP BY dtype")}
    auto = common.fetch_all(
        "SELECT COUNT(*) AS n FROM t_dispute WHERE stage='ai' AND status='resolved'")[0]["n"] or 0
    decided = (by_status.get("resolved", 0) + by_status.get("overturned", 0))
    lost = common.fetch_all(
        "SELECT COUNT(*) AS n FROM t_dispute WHERE liability='merchant' "
        "AND status IN ('resolved','overturned')")[0]["n"] or 0

    dur = common.fetch_all(
        "SELECT created_at, closed_at FROM t_dispute WHERE closed_at IS NOT NULL")
    hours = []
    for r in dur:
        a, b = _parse(r["created_at"]), _parse(r["closed_at"])
        if a and b:
            hours.append((b - a).total_seconds() / 3600)

    pen = common.fetch_all("SELECT ptype, COUNT(*) AS n FROM t_penalty "
                           "WHERE status='active' AND ptype<>'record' GROUP BY ptype")
    pts = common.fetch_all("SELECT COALESCE(SUM(points),0) AS p FROM t_penalty "
                           "WHERE status='active'")[0]["p"] or 0
    ap = {r["status"]: r["n"] for r in common.fetch_all(
        "SELECT status, COUNT(*) AS n FROM t_appeal GROUP BY status")}
    ap_all = sum(ap.values())
    shops = {r["status"]: r["n"] for r in common.fetch_all(
        "SELECT status, COUNT(*) AS n FROM t_shop_credit GROUP BY status")}

    risk = common.fetch_all(
        """SELECT c.shop_id, s.name, c.credit_score, c.level, c.status,
                  c.dispute_count, c.dispute_lost, c.points_year, c.suspend_until
           FROM t_shop_credit c JOIN t_shop s ON s.id=c.shop_id
           ORDER BY c.points_year DESC, c.dispute_lost DESC LIMIT 6""")

    return {
        "disputes": total,
        "by_status": by_status,
        "by_type": by_type,
        "pending_human": by_status.get("pending_human", 0) + by_status.get("arbitrating", 0),
        "ai_auto": auto,
        "ai_auto_rate": round(auto / decided, 4) if decided else 0,
        "merchant_lost": lost,
        "merchant_lost_rate": round(lost / total, 4) if total else 0,
        "avg_handle_hours": round(sum(hours) / len(hours), 1) if hours else 0,
        "penalties": {r["ptype"]: r["n"] for r in pen},
        "points_total": round(float(pts), 1),
        "appeals": ap,
        "appeal_total": ap_all,
        "appeal_rate": round(ap_all / decided, 4) if decided else 0,
        "appeal_adopted": ap.get("accepted", 0),
        "appeal_adopt_rate": round(ap.get("accepted", 0) / ap_all, 4) if ap_all else 0,
        "shops": shops,
        "risk_shops": risk,
        "threshold": AUTO_CONF,
    }


CS_STATUS_LABEL = {"bot": "机器人值守", "agent": "人工接待",
                   "escalated": "已转平台介入", "closed": "已结束"}


def _cs_brief(sid: str | None, limit: int = 60) -> dict | None:
    """来源客服会话摘要。仲裁员要能看到「买家当初是怎么说的」。

    只读展示，不参与任何裁决计算：AI 定责仍然只看结构化字段和证据，
    否则一段措辞激烈的对话就能把责任判给商家。
    """
    sid = str(sid or "").strip()
    if not sid:
        return None
    rows = common.fetch_all(
        "SELECT id, shop_id, buyer_name, channel, status, msg_count, created_at, updated_at "
        "FROM t_cs_session WHERE id=%s", (sid,))
    if not rows:
        return None
    s = dict(rows[0])
    msgs = common.fetch_all(
        "SELECT role, content, source, created_at FROM t_cs_message WHERE session_id=%s "
        "ORDER BY id ASC LIMIT %s", (sid, limit))
    s["messages"] = [{**m, "created_at": str(m.get("created_at") or "")} for m in msgs]
    s["status_label"] = CS_STATUS_LABEL.get(s.get("status") or "", s.get("status") or "")
    s["created_at"] = str(s.get("created_at") or "")
    s["updated_at"] = str(s.get("updated_at") or "")
    return s


def detail(dispute_id: int) -> dict:
    d = get(dispute_id)
    if not d:
        raise ValueError("纠纷不存在")
    msgs = common.fetch_all(
        "SELECT * FROM t_dispute_msg WHERE dispute_id=%s ORDER BY id ASC LIMIT 200",
        (dispute_id,))
    pens = common.fetch_all(
        "SELECT * FROM t_penalty WHERE dispute_id=%s ORDER BY id ASC", (dispute_id,))
    aps = common.fetch_all(
        "SELECT * FROM t_appeal WHERE dispute_id=%s ORDER BY id ASC", (dispute_id,))
    shop = common.fetch_all("SELECT id, name, platform FROM t_shop WHERE id=%s",
                            (d["shop_id"],))
    c = credit(int(d["shop_id"]))
    return {
        "dispute": {**d,
                    "evidence": evidence_of(d),
                    "claim_amount": float(d.get("claim_amount") or 0),
                    "ai_amount": float(d.get("ai_amount") or 0),
                    "ai_points": float(d.get("ai_points") or 0),
                    "ai_confidence": float(d.get("ai_confidence") or 0),
                    "created_at": str(d.get("created_at") or ""),
                    "updated_at": str(d.get("updated_at") or ""),
                    "verdict_at": str(d.get("verdict_at") or ""),
                    "sla_due": str(d.get("sla_due") or ""),
                    "type_label": DTYPES.get(d["dtype"], {}).get("label", d["dtype"]),
                    "remedy_label": REMEDY_LABEL.get(d.get("ai_remedy") or "", ""),
                    },
        "messages": [{**m, "created_at": str(m.get("created_at") or "")} for m in msgs],
        "penalties": [{**p, "points": float(p.get("points") or 0),
                       "type_label": PENALTY_LABEL.get(p["ptype"], p["ptype"]),
                       "effective_at": str(p.get("effective_at") or ""),
                       "expire_at": str(p.get("expire_at") or "")} for p in pens],
        "appeals": [{**x, "ai_confidence": float(x.get("ai_confidence") or 0),
                     "created_at": str(x.get("created_at") or "")} for x in aps],
        "shop": shop[0] if shop else None,
        "order": order_detail(d.get("order_no") or ""),
        "session": _cs_brief(d.get("session_id")),
        "credit": {**c, "status_label": STATUS_LABEL.get(c["status"], c["status"])},
        "analysis": analyze(d),
    }


# ---------------------------------------------------------------- 演示数据

DEMO = [
    # (买家键, 店铺序号, 类型, 标题, 诉求, 金额, 凭证)
    ("lichen", 1, "description", "收到的台灯与页面描述材质不符",
     "页面写实木底座，收到是塑料的，要求退货退款并承担运费", 268.00,
     [{"k": "商品实拍", "v": "底座侧面可见注塑合模线"},
      {"k": "商品详情截图", "v": "详情页标注「实木底座」"}]),
    ("lichen", 0, "aftersale", "提交退货退款三天无人审核",
     "申请退货退款后商家一直不处理，超过 36 小时", 159.00,
     [{"k": "售后申请截图", "v": "售后单 20261027001，状态待商家处理"},
      {"k": "聊天记录", "v": "客服回复「稍等」后无下文"}]),
    ("lichen", 1, "counterfeit", "购买的耳机疑似假冒品牌",
     "包装无防伪码，音质异常，要求退款并按规则处理商家", 899.00,
     [{"k": "商品实拍", "v": "包装盒无防伪涂层，序列号印刷模糊"},
      {"k": "检测报告", "v": "第三方鉴定为仿品（报告号 JD20261028）"}]),
    ("lichen", 0, "service", "客服长时间不回复且态度推诿",
     "咨询尺码问题连续两天无有效回复", 0.0,
     [{"k": "聊天记录", "v": "10-26 至 10-28 共 5 条未读"}]),
    ("lichen", 1, "quality", "收到时外箱破损内部有裂痕",
     "要求退货退款，运费由商家承担", 128.00,
     [{"k": "商品实拍", "v": "内件边角开裂约 3cm"},
      {"k": "聊天记录", "v": "商家称「运输问题找快递」"}]),
]


def _seed_order(shop_name: str, dtype: str, amount: float) -> str:
    """给演示纠纷造一张真实订单。

    为什么演示也要造订单：判「有没有超时」只能看订单时间戳。没有订单的纠纷，
    AI 只能复述买家那句「三天没发货」，那不叫定责，叫传话。

    商品明细按纠纷类型给：买家投诉「实木底座是塑料的」，订单上得真有
    「北欧实木底座台灯」这件商品，仲裁员才能核对描述与实物；
    只有金额的话，这条诉求和订单之间没有任何可验证的连接。
    """
    now = dt.datetime.now()

    def back(days: float = 0, hours: float = 0) -> str:
        return (now - dt.timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    paid, shipped, recv = back(days=6), back(days=5, hours=20), back(days=3)
    status, aft_type, aft_at, remark = "已完成", "", "", ""
    category = "家居"
    item_title, sku = "北欧实木底座台灯", "原木色 / E27 暖光 / 高 45cm"
    carrier, tracking_kind = "顺丰速运", "SF"
    picked_offset, no_pick = 4.0, False

    if dtype == "aftersale":
        status, aft_type, aft_at = "售后中", "退货退款", back(days=2, hours=12)
        remark = "买家提交退货退款申请，等待商家审核"
        item_title, sku = "全棉四件套", "1.8m 床 / 灰蓝格"
        carrier, tracking_kind = "中通快递", "ZT"
    elif dtype == "counterfeit":
        paid, shipped, recv = back(days=9), back(days=8, hours=10), back(days=6)
        status, aft_type, aft_at = "售后中", "仅退款", back(days=4)
        remark = "买家主张商品为假冒，已上传检测报告"
        category = "数码"
        item_title, sku = "无线蓝牙耳机 Pro", "白色 / 主动降噪版"
        carrier, tracking_kind = "中通快递", "ZT"
        picked_offset = 6.0
    elif dtype == "quality":
        paid, shipped, recv = back(days=4), back(days=3, hours=14), back(days=1, hours=6)
        remark = "外箱破损，买家已拍照留存"
        item_title, sku = "实木边几", "胡桃色 / 40×40cm"
    elif dtype == "service":
        paid, shipped, recv = back(days=8), back(days=7), back(days=5)
        remark = "买家就尺码问题咨询客服"
        category = "服饰"
        item_title, sku = "针织开衫", "杏色 / M 码"
    elif dtype == "shipping":
        paid, shipped, recv, status = back(days=3), back(days=0, hours=12), "", "已发货"
        remark = "买家催发货，商家称已交快递"
        item_title, sku = "厨房置物架", "三层 / 免打孔"
        carrier, tracking_kind = "圆通速递", "YT"
        # 点了「发货」但物流一直没揽收：规则 2.4 里"虚假发货"的典型形态。
        # 演示数据故意造出这一单，否则这个判定在页面上永远看不到。
        no_pick = True

    # 商品必须和这家店卖的东西一致。上面类型分支给的是"这种纠纷的典型商品"，
    # 但一家数码店不会卖实木边几——订单上出现它，整张单就不可信了，
    # 而"带单核查"要的恰恰是可信的事实底座。
    sc = common.fetch_all("SELECT category FROM t_shop WHERE name=%s", (shop_name,))
    if sc and (sc[0].get("category") or "").strip():
        category = sc[0]["category"]
        item_title, sku, carrier, tracking_kind = _item_for(category)

    n = int(common.fetch_all("SELECT COUNT(*) AS n FROM t_order")[0]["n"] or 0)
    while True:
        no = f"SO{now:%Y%m%d}{(n + 1) % 1000000:06d}"
        if not common.fetch_all("SELECT 1 AS x FROM t_order WHERE order_no=%s", (no,)):
            break
        n += 1

    amt = round(float(amount or 0), 2)
    qty = 1
    tracking = f"{tracking_kind}{now:%y%m%d}{(n % 100000) * 7 + 100000:06d}"
    picked_at = None
    if shipped and not no_pick:
        picked_at = (dt.datetime.strptime(shipped, "%Y-%m-%d %H:%M:%S")
                     + dt.timedelta(hours=picked_offset)).strftime("%Y-%m-%d %H:%M:%S")

    common.insert(
        """INSERT INTO t_order (order_no, shop_name, category, amount, status,
               paid_at, shipped_at, received_at, aftersale_type, aftersale_applied_at,
               item_title, sku, qty, unit_price, carrier, tracking_no, picked_at, remarks)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (no, shop_name[:64], category, amt, status,
         paid, shipped or None, recv or None, aft_type or None, aft_at or None,
         item_title, sku, qty, round(amt / qty, 2) if qty else amt,
         carrier, tracking if shipped else "", picked_at, remark))
    return no


def backfill_orders() -> int:
    """已经灌过演示纠纷的库，补上订单号。

    老数据只有买家口述、没有订单事实，平台仲裁页就只能显示一段自述——
    这正是「平台界面看起来和商家页面一样」的根因：三方没有共同的锚点。
    """
    rows = common.fetch_all(
        "SELECT d.id, d.dtype, d.claim_amount, s.name AS shop_name "
        "FROM t_dispute d LEFT JOIN t_shop s ON s.id=d.shop_id "
        "WHERE (d.order_no IS NULL OR d.order_no='') ORDER BY d.id")
    n = 0
    for r in rows:
        try:
            no = _seed_order(r.get("shop_name") or "", r["dtype"],
                             float(r.get("claim_amount") or 0))
            common.execute("UPDATE t_dispute SET order_no=%s WHERE id=%s", (no, r["id"]))
            n += 1
        except Exception as e:
            print("[gov] 补订单失败:", str(e)[:80])
    return n


def _item_for(cat: str) -> tuple[str, str, str, str]:
    """按**店铺类目**推商品，而不是按订单上的 category 字段。

    订单的 category 是造数时随手写的，和店铺卖什么没有关系，于是出现过
    「梦境数码专营店的订单里是一件霞光实木边几」——商品名里还带别家店的字号。
    这种数据出现在「带单核查」里，仲裁员第一眼就会怀疑整张单是编的。
    """
    c = cat or ""
    for keys, v in (
        (("3C", "数码", "电子", "手机"), ("无线蓝牙耳机 Pro", "白色 / 主动降噪版", "中通快递", "ZT")),
        (("服饰", "服装", "鞋", "箱包"), ("针织开衫", "杏色 / M 码", "圆通速递", "YT")),
        (("日用", "百货", "厨", "清洁"), ("厨房置物架", "三层 / 免打孔", "圆通速递", "YT")),
        (("美妆", "个护"), ("氨基酸洁面乳", "120g / 温和型", "圆通速递", "YT")),
        (("食品", "生鲜", "饮料"), ("混合坚果礼盒", "750g / 6 袋", "顺丰速运", "SF")),
        (("母婴",), ("婴儿纸尿裤", "L 码 / 54 片", "京东物流", "JD")),
        (("运动", "户外"), ("速干运动 T 恤", "深灰 / L 码", "中通快递", "ZT")),
        (("家居", "家纺", "家装"), ("实木边几", "胡桃色 / 40×40cm", "顺丰速运", "SF")),
    ):
        if any(k in c for k in keys):
            return v
    return ("实木边角置物架", "原木色 / 60cm", "顺丰速运", "SF")


def _accessory_for(cat: str) -> tuple[str, str]:
    """同一类目的常见配件。多商品订单里第二行不是随机商品，而是真实会一起买的东西：
    「耳机 + 保护套」合理，「耳机 + 实木边几」一眼就是造的。
    """
    c = cat or ""
    for keys, v in (
        (("3C", "数码", "电子", "手机"), ("耳机硅胶保护套", "透明 / 通用")),
        (("服饰", "服装", "鞋", "箱包"), ("同色系替换鞋带", "杏色 / 120cm")),
        (("日用", "百货", "厨", "清洁"), ("免钉挂钩（6 只装）", "白色")),
        (("美妆", "个护"), ("旅行分装瓶（3 只）", "50ml")),
        (("食品", "生鲜", "饮料"), ("独立分装袋（10 只）", "食品级")),
        (("母婴",), ("婴儿湿巾（80 抽）", "无香型")),
        (("运动", "户外"), ("运动吸汗腕带", "深灰")),
        (("家居", "家纺", "家装"), ("防滑脚垫（4 只装）", "深棕 / 40mm")),
    ):
        if any(k in c for k in keys):
            return v
    return ("安装配件包", "通用")


def backfill_order_items() -> int:
    """给老订单补商品明细、运单号，并把明细落进 t_order_item。

    三件事一次做完，是因为它们依赖同一份推导结果（类目 → 商品 → 承运商）：
    分开写会出现「主表写耳机、子表写边几」「运单号前缀和承运商对不上」。

    必须批量写：这张表有几千单，逐条 execute 在 SQLite 下每条一个事务，
    启动会被拖到几分钟。第一次运行把存量灌进去，之后靠 NOT EXISTS 直接跳过。
    """
    rows = common.fetch_all(
        """SELECT o.order_no, o.category, o.amount, o.shipped_at, o.item_title,
                  s.category AS shop_cat
           FROM t_order o LEFT JOIN t_shop s ON s.name=o.shop_name
           WHERE o.item_title IS NULL OR o.item_title=''
                 OR substr(o.item_title, 1, 2)='霞光'
                 OR NOT EXISTS (SELECT 1 FROM t_order_item i
                                WHERE i.order_no=o.order_no)
           ORDER BY o.id""")
    ts = _now()
    updates: list[tuple] = []
    item_rows: list[tuple] = []
    for r in rows:
        title, sku, carrier, kind = _item_for(r.get("shop_cat") or r.get("category") or "")
        no = str(r["order_no"])
        # 单号由订单号后 8 位派生：可重现，不会每次刷新换一个
        tracking = f"{kind}{abs(hash(no)) % 90000000 + 10000000:09d}"
        picked = None
        if r.get("shipped_at"):
            # 老数据一律按「发货后 4 小时揽收」补齐：它们本来就不是虚假发货样本，
            # 凭空造出一批"无揽收"订单，会让所有历史案子的扣分凭空变重。
            try:
                picked = (dt.datetime.strptime(str(r["shipped_at"]), "%Y-%m-%d %H:%M:%S")
                          + dt.timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                picked = None
        amt = float(r.get("amount") or 0)
        cat = r.get("shop_cat") or r.get("category") or ""
        updates.append((title, sku, amt, carrier,
                        tracking if r.get("shipped_at") else "", picked, no))
        # 商品行：金额够大的单拆成「主件 + 配件」两行。一单一品看不出子表的意义，
        # 而售后争议恰恰多半是「整单两件，只退其中一件」。两行合计严格等于实付，
        # 否则 amount_mismatch 会对一批正常订单报「金额不一致」——噪声比问题更糟。
        irows = [(title, sku, 1, amt)]
        if amt >= 200:
            a_title, a_sku = _accessory_for(cat)
            main = round(amt * 0.85, 2)
            irows = [(title, sku, 1, main), (a_title, a_sku, 1, round(amt - main, 2))]
        for i, (t_, s_, q_, u_) in enumerate(irows, 1):
            item_rows.append((no, i, t_, s_, int(q_), round(float(u_), 2), cat, ts))

    n = 0
    if updates:
        try:
            n += common.execute_many(
                "UPDATE t_order SET item_title=%s, sku=%s, qty=1, unit_price=%s, "
                "carrier=%s, tracking_no=%s, picked_at=%s WHERE order_no=%s", updates)
        except Exception as e:
            print("[gov] 补订单明细失败:", str(e)[:80])
    if item_rows:
        # 再挡一次重复：主查询和写入之间可能有并发补数（定时任务 + 启动），
        # UNIQUE(order_no, line_no) 撞了会让整批失败，先按已有订单号过滤。
        have = {str(x["order_no"]) for x in common.fetch_all(
            "SELECT order_no FROM t_order_item")}
        item_rows = [x for x in item_rows if str(x[0]) not in have]
        if item_rows:
            try:
                n += common.execute_many(
                    "INSERT INTO t_order_item (order_no, line_no, title, sku, qty, "
                    "unit_price, category, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", item_rows)
            except Exception as e:
                print("[gov] 写商品行失败:", str(e)[:80])

    # 已经挂了纠纷的订单，商品要和店铺类目对得上。老数据是按"纠纷类型"给的
    # 商品（家居店卖针织开衫），撤不掉；这里只纠正纠纷单关联的那几张，
    # 不碰历史订单池，否则一次启动会改写几千行。
    fixes, ifix = [], []
    for r in common.fetch_all(
            """SELECT o.order_no, o.item_title, s.category
               FROM t_order o JOIN t_dispute d ON d.order_no=o.order_no
               LEFT JOIN t_shop s ON s.name=o.shop_name"""):
        title, sku, carrier, _k = _item_for(r.get("category") or "")
        if title != (r.get("item_title") or ""):
            fixes.append((title, sku, carrier, r["order_no"]))
            ifix.append((title, sku, r["order_no"]))
    if fixes:
        try:
            common.execute_many(
                "UPDATE t_order SET item_title=%s, sku=%s, carrier=%s WHERE order_no=%s",
                fixes)
            # 子表首行跟着改，否则「主表写耳机、子表写边几」，
            # 页面和带单核查各显示一个商品，比原来只错一处更难查。
            common.execute_many(
                "UPDATE t_order_item SET title=%s, sku=%s "
                "WHERE order_no=%s AND line_no=1", ifix)
            n += len(fixes)
        except Exception as e:
            print("[gov] 纠正商品失败:", str(e)[:80])
    return n


# 演示会话的台词。写具体的原话而不是"买家反馈了问题"，
# 是因为仲裁员真正需要判断的是"这句话算不算明确诉求"。
_CS_LINES: dict[str, list[tuple[str, str]]] = {
    "description": [
        ("buyer", "你们页面写的是实木，我收到的明显是贴皮的，边角已经在起皮了"),
        ("bot", "亲，商品材质以详情页参数为准，不同批次可能存在细微差异哦～"),
        ("buyer", "不是细微差异。详情页参数就写着「实木」，我截图了，可以发给你看"),
        ("agent", "您好，我是店里售后。麻烦发一下实物照片，我这边跟仓库核对一下"),
        ("buyer", "照片发了，起皮的位置很清楚。这属于描述不符，我要退货退款，运费你们出"),
        ("agent", "这个我需要问仓库，可能要两三天才能回复您"),
        ("buyer", "已经拖了两天。我不想再等了，直接申请平台介入"),
    ],
    "shipping": [
        ("buyer", "下单三天了，一直显示待发货，到底什么时候发"),
        ("bot", "亲，我们下单后 48 小时内安排出库，节假日顺延哦～"),
        ("buyer", "现在早就超过 48 小时了，你算算时间"),
        ("agent", "不好意思，这款仓库在调货，预计明天发出"),
        ("buyer", "昨天也是这么说的。我孩子生日已经过了，东西还没到，我要退款"),
    ],
    "aftersale": [
        ("buyer", "退货申请提交两天了，一直没人审核"),
        ("bot", "亲，售后申请会在 36 小时内处理，请耐心等待～"),
        ("buyer", "36 小时也过了。你们就是拖着不处理，想让系统自动关闭吗"),
        ("agent", "这边帮您催一下"),
        ("buyer", "催了两天了。按平台规则逾期未处理视为同意，我要求平台介入执行"),
    ],
    "counterfeit": [
        ("buyer", "这个包是假货，五金件和正品完全不一样，logo 的字都歪了"),
        ("agent", "我们是品牌授权渠道，不可能是假货"),
        ("buyer", "我拿去专柜问过了，人家说没见过这个批次。检测报告我也能出"),
        ("agent", "您先寄回来我们看看"),
        ("buyer", "寄回去你们就说是我弄坏的。我要求平台介入，按规定假一赔三"),
    ],
    "quality": [
        ("buyer", "收到就裂了，外箱也是破的"),
        ("bot", "亲，签收时建议当面验货哦，破损可以拒收～"),
        ("buyer", "快递直接放门口，根本没让我验。东西现在就是坏的"),
        ("agent", "可以给您补发一个"),
        ("buyer", "不要补发，我要退货退款，运费你们承担"),
    ],
    "service": [
        ("buyer", "问个尺码问题，问了三次都没人回"),
        ("bot", "亲，客服在线时间是 9:00-21:00 哦～"),
        ("buyer", "现在就是下午三点，在线时间，还是没人理"),
        ("agent", "在的，请问什么问题"),
        ("buyer", "已经不想问了。这种服务态度我要投诉到平台"),
    ],
}


def _seed_cs_session(buyer: dict, shop: dict, dtype: str, hours_ago: float = 27) -> str:
    """造一段客服会话，作为演示纠纷的来源。

    纠纷如果全是"凭空出现"的，仲裁台就看不到买家最初是怎么说的，
    三方对接等于缺了一方。会话状态直接置 escalated，因为它确实已经转成了纠纷。
    """
    sid = schema.new_sid()
    t0 = dt.datetime.now() - dt.timedelta(hours=hours_ago)
    lines = _CS_LINES.get(dtype) or [
        ("buyer", "我买的东西有问题，你们怎么处理"),
        ("bot", "亲，说说具体是什么问题，我帮您转售后～"),
        ("buyer", "说不清楚，我直接找平台"),
    ]
    ts = t0.strftime("%Y-%m-%d %H:%M:%S")
    common.execute(
        "INSERT INTO t_cs_session (id, shop_id, buyer_id, buyer_name, buyer_tag, channel, "
        "status, last_msg, msg_count, handoff, created_at, updated_at) "
        "VALUES (%s,%s,%s,%s,'','淘宝','escalated','',0,1,%s,%s)",
        (sid, shop["id"], buyer["id"], buyer.get("display_name") or "买家", ts, ts))
    for i, (role, text) in enumerate(lines):
        at = (t0 + dt.timedelta(minutes=4 * i)).strftime("%Y-%m-%d %H:%M:%S")
        common.insert(
            "INSERT INTO t_cs_message (session_id, role, content, source, score, faq_id, "
            "intent, latency_ms, created_at) VALUES (%s,%s,%s,%s,0,NULL,'',0,%s)",
            (sid, role, text, {"bot": "faq", "agent": "human"}.get(role, ""), at))
    common.execute(
        "UPDATE t_cs_session SET msg_count=%s, last_msg=%s, updated_at=%s WHERE id=%s",
        (len(lines), lines[-1][1][:250],
         (t0 + dt.timedelta(minutes=4 * len(lines))).strftime("%Y-%m-%d %H:%M:%S"), sid))
    return sid


def backfill_sessions(limit: int = 5) -> int:
    """给已有的演示纠纷补上「来源客服会话」（老库升级用，可重复执行）。"""
    rows = common.fetch_all(
        "SELECT d.id, d.dtype, d.shop_id, d.buyer_id FROM t_dispute d "
        "WHERE (d.session_id IS NULL OR d.session_id='') AND d.buyer_id IS NOT NULL "
        "ORDER BY d.id LIMIT %s", (limit,))
    n = 0
    for r in rows:
        try:
            shop = common.fetch_all("SELECT id, name FROM t_shop WHERE id=%s",
                                    (r["shop_id"],))
            buyer = common.fetch_all("SELECT id, display_name FROM t_user WHERE id=%s",
                                     (r["buyer_id"],))
            if not shop or not buyer:
                continue
            sid = _seed_cs_session(dict(buyer[0]), dict(shop[0]), r["dtype"])
            common.execute("UPDATE t_dispute SET session_id=%s WHERE id=%s", (sid, r["id"]))
            n += 1
        except Exception as e:
            print("[gov] 补来源会话失败:", str(e)[:80])
    return n


def seed_demo() -> int:
    """首次启动灌几条真实感的纠纷。空白的仲裁台既没法演示，也看不出链路是否通。"""
    if common.fetch_all("SELECT COUNT(*) AS n FROM t_dispute")[0]["n"] > 0:
        return 0
    users = {u["username"]: u for u in common.fetch_all(
        "SELECT id, username, display_name, role, shop_id FROM t_user")}
    shops = common.fetch_all("SELECT id, name FROM t_shop ORDER BY id")
    if not shops or not users:
        return 0
    n = 0
    made: list[dict] = []
    for uname, si, dtype, title, claim, amt, ev in DEMO:
        u = users.get(uname)
        if not u or si >= len(shops):
            continue
        try:
            # 先造订单再立案：纠纷从一开始就带着订单号，仲裁页才有事实底座。
            ono = _seed_order(shops[si]["name"], dtype, amt)
            # 再造一段客服会话：纠纷要看得出「买家最初是怎么说的」
            sid = _seed_cs_session(dict(u, display_name=u["display_name"] or uname),
                                   shops[si], dtype)
            r = create(int(shops[si]["id"]), dtype, title, claim, amt, ev,
                       dict(u, display_name=u["display_name"] or uname), session_id=sid,
                       order_no=ono)
            made.append(r)
            n += 1
        except Exception as e:
            print("[gov] 演示纠纷生成失败:", str(e)[:80])

    # 给第一条自动裁决的纠纷补一个商家申诉，让"申诉复核"这条链路也是活的
    try:
        auto = [m for m in made if m.get("status") == "resolved" and m.get("points")]
        if auto:
            d = get(int(auto[0]["id"]))
            p = common.fetch_all("SELECT * FROM t_penalty WHERE dispute_id=%s AND points>0 "
                                 "ORDER BY id LIMIT 1", (d["id"],))
            merchant = common.fetch_all(
                "SELECT * FROM t_user WHERE shop_id=%s AND role='merchant' LIMIT 1",
                (d["shop_id"],))
            if p and merchant:
                appeal_create("penalty", int(p[0]["id"]), merchant[0],
                              "该商品页面已标注「实木贴皮」，买家截图为历史版本，"
                              "请求复核适用条款 2.2 是否成立",
                              [{"k": "商品详情历史快照", "v": "10-01 版本已注明贴皮工艺"}])
    except Exception as e:
        print("[gov] 演示申诉生成失败:", str(e)[:80])
    return n
