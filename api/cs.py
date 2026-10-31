"""智能客服接口层。

两类调用方，权限模型完全不同：

  * 买家侧（/cs 页面，模拟商家后台的买家咨询窗口）
    只能操作**自己刚创建的**会话，看到的是"我和这家店的对话"。

  * 坐席侧（/service 页面，商家客服工作台）
    商家只能看自己店铺的会话（shop_id 隔离），ops/admin 看全平台。
    越权一律返回 404 而不是 403 —— 403 等于告诉对方"这个会话存在，只是你没权限"，
    把别人有多少客户、正在聊什么泄漏出去了。

阈值、模型、知识库这类配置只有 ops/admin 能改。
商家能改自己的知识库（那是他店里的业务话术），但不能改全局阈值。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import common
import service.cs as cs
import service.faq as faq
import service.gov as gov
from api import schema, security as sec

router = APIRouter(prefix="/api/cs", tags=["cs"])


# ---------------------------------------------------------------- 工具

def _scope(user: dict) -> int | None:
    """返回该用户可见的 shop_id；None 表示全平台。

    买家不能进坐席体系：以前这里对非商家一律返回 None，等于「全平台可见」，
    买家登录后能翻到所有店铺的会话甚至接管。角色不同，可见范围就必须不同，
    否则「三方对接」只是个说法——数据层面根本没收口。
    """
    role = user["role"]
    if role == "merchant":
        return user["shop_id"]
    if role in sec.AGENT_ROLES:
        return None
    raise HTTPException(403, "买家账号没有坐席权限")


def _guard(sess: dict, user: dict) -> dict:
    # 规则 / 治理板块的会话没有店铺，性质是"我和平台助手的对话"，
    # 只能由发起人自己看。套用店铺那套 shop_id 归属判断会全部 404——
    # 因为它们的 shop_id 是 NULL，永远不等于商人的 shop_id。
    if (sess.get("scope") or "shop") != "shop":
        if sess.get("buyer_id") == user["id"]:
            return sess
        raise HTTPException(404, "会话不存在")

    if user["role"] == "buyer":
        # 买家只能看自己发起的会话。以前写成 `if sess.get("buyer_id") and ...`，
        # buyer_id 为 NULL 的历史/演示会话会被直接放行——任何买家都能翻到别人的会话，
        # 四个端的数据就串了。这里必须严格相等。
        if sess.get("buyer_id") != user["id"]:
            raise HTTPException(404, "会话不存在")
        return sess
    shop = _scope(user)
    if shop is not None and sess.get("shop_id") != shop:
        raise HTTPException(404, "会话不存在")
    return sess


def _own(sid: str, user: dict) -> dict:
    s = cs.get_session(sid)
    if not s:
        raise HTTPException(404, "会话不存在")
    return _guard(s, user)


def _msg(m: dict) -> dict:
    return {"id": m["id"], "role": m["role"], "content": m.get("content") or "",
            "sender_role": m.get("sender_role") or "",
            "source": m.get("source") or "", "score": float(m.get("score") or 0),
            "faq_id": m.get("faq_id"), "intent": m.get("intent") or "",
            "latency_ms": m.get("latency_ms") or 0,
            "created_at": str(m.get("created_at") or "")}


# ---------------------------------------------------------------- 买家侧

# 角色默认进哪个板块。这是整套设计的入口约定：
# 买家进店铺客服，商家进平台规则助手，运营进治理规范助手。
# 以前三者共用一个窗口，才出现"商家打开咨询口，看到的是自己的店在接待自己"。
DEFAULT_SCOPE = {"buyer": "shop", "merchant": "rule", "ops": "gov", "admin": "gov"}

# 谁能用哪些板块。买家只能问店铺；商家额外能问规则；运营/admin 三个都能进
# （运营需要预览店铺客服，也需要问治理口径）。
ALLOWED_SCOPES = {
    "buyer": {"shop"},
    "merchant": {"shop", "rule"},
    "ops": {"shop", "rule", "gov"},
    "admin": {"shop", "rule", "gov"},
}


class NewSessionIn(BaseModel):
    buyer_name: str | None = None
    channel: str | None = None
    shop_id: int | None = None
    scope: str | None = None


class SayIn(BaseModel):
    text: str


@router.post("/sessions")
def create_session(body: NewSessionIn, user: dict = Depends(sec.current_user)):
    role = user["role"]
    scope = (body.scope or "").strip() or DEFAULT_SCOPE.get(role, "shop")
    if scope not in cs.SCOPES:
        raise HTTPException(400, "未知的咨询板块")
    if role not in ALLOWED_SCOPES or scope not in ALLOWED_SCOPES[role]:
        raise HTTPException(403, f"{sec.ROLE_LABEL.get(role, role)}不能使用该咨询板块")

    if scope == "shop":
        # 店铺客服必须落在某个具体店铺上：运营预览时也要选店，否则
        # 系统不知道"以哪家店的身份"接待。
        shop = body.shop_id or (user["shop_id"] if role == "merchant" else None)
        if not shop:
            raise HTTPException(400, "请选择要咨询的店铺")
        if role == "merchant" and shop != user["shop_id"]:
            raise HTTPException(403, "只能预览自己店铺的客服")
        name = body.buyer_name or user["display_name"]
        channel = body.channel or "淘宝"
    else:
        # 规则 / 治理板块不挂店铺，提问人就是当前登录账号本人。
        shop = None
        name = user["display_name"]
        channel = "站内"

    try:
        s = cs.new_session(shop, name, channel, buyer_id=user["id"], scope=scope)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**s, "shop_id": shop, "scope": scope}


@router.post("/sessions/{sid}/messages")
def say(sid: str, body: SayIn, user: dict = Depends(sec.current_user)):
    sess = _own(sid, user)
    try:
        # 把真实身份传下去：规则咨询里发言的是商家、治理咨询里是平台运营，
        # 不能一律记成 buyer，否则对话里商家自己的消息会被标成「买家」。
        res = cs.handle(sess["id"], body.text, user["role"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(429, str(e))
    res["session_id"] = sid
    return res


@router.get("/sessions/{sid}")
def get_session(sid: str, user: dict = Depends(sec.current_user)):
    s = _own(sid, user)
    rows = common.fetch_all(
        "SELECT * FROM t_cs_message WHERE session_id=%s ORDER BY id ASC LIMIT 400", (sid,))
    dis = cs.dispute_of(sid)
    # 能不能转介：已转过的、没有店铺的、以及"商家自己在自己店里模拟买家"的，
    # 都不该给按钮。最后一种点了必然被 gov.create 拒（不能投诉自己经营的店铺），
    # 与其让坐席点出一个 400，不如一开始就不显示。
    can = cs.scope_of(s) == "shop" and bool(s.get("shop_id")) and not dis
    if user["role"] == "merchant" and s.get("buyer_id") == user["id"]:
        can = False
    return {"session": {"id": s["id"], "buyer_name": s["buyer_name"],
                        "channel": s["channel"], "status": s["status"],
                        "scope": cs.scope_of(s),
                        "handoff": s["handoff"], "msg_count": s["msg_count"],
                        "buyer_id": s.get("buyer_id"),
                        "updated_at": str(s["updated_at"] or "")},
            "dispute": dis, "can_escalate": can,
            "messages": [_msg(m) for m in rows]}


class EscalateIn(BaseModel):
    """转平台介入。字段都可空：空的时候由服务端从会话内容里补。"""
    dtype: str | None = ""
    claim: str | None = ""
    title: str | None = ""
    claim_amount: float | None = 0
    order_no: str | None = ""
    claim_type: str | None = "refund"
    evidence: list[dict] | None = None


@router.post("/sessions/{sid}/escalate")
def escalate(sid: str, body: EscalateIn, user: dict = Depends(sec.current_user)):
    """会话一键转平台介入。

    坐席和买家都能调：坐席在客服工作台点「转平台介入」，买家在自己的
    咨询窗口点「申请平台介入」。同一会话重复提交返回原单，不重复建。
    """
    sess = _own(sid, user)
    ev = [{"k": str(e.get("k"))[:30], "v": str(e.get("v") or "")[:300]}
          for e in (body.evidence or []) if isinstance(e, dict) and e.get("k")]
    try:
        return cs.escalate(sess["id"], user, body.dtype or "", body.claim or "",
                           float(body.claim_amount or 0), body.order_no or "",
                           body.title or "", body.claim_type or "refund", ev)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/sessions/{sid}/dispute")
def session_dispute(sid: str, user: dict = Depends(sec.current_user)):
    _own(sid, user)
    return {"dispute": cs.dispute_of(sid)}


@router.get("/sessions/{sid}/prefill")
def escalate_prefill(sid: str, user: dict = Depends(sec.current_user)):
    """转介表单的预填：类型猜测、对话稿、平台可核验的证据。"""
    sess = _own(sid, user)
    text = cs.transcript(sid)
    dtype = cs.guess_dtype(text)
    return {"dtype": dtype, "type_label": gov.DTYPES[dtype]["label"],
            "transcript": text,
            "evidence": [{"k": "聊天记录", "v": f"客服会话 {sid}（系统自动带入）"}],
            "shop_id": sess.get("shop_id"), "buyer_name": sess.get("buyer_name")}


# ---------------------------------------------------------------- 带单核查（仅治理板块）
#
# 治理助手回答"条款怎么规定"，但不回答"这一单该不该罚"。两者之间还差一步：
# 把这一单的事实摆出来。这一步以前完全没有——运营想问某一张单，只能自己去
# 仲裁台翻，或者在窗口里笼统地问"这个案子怎么判"（助手必然拒答）。

class CaseIn(BaseModel):
    dispute_id: int


@router.get("/cases")
def case_options(limit: int = Query(8, le=30),
                 user: dict = Depends(sec.require("ops", "admin"))):
    """带单核查的可选案件。只有平台侧能看：买家看到别人的案子就是数据泄漏。"""
    return {"items": cs.cases_for_ops(limit),
            "note": "在办案件优先；已裁决的排在后面，供复核与申诉时回看"}


@router.post("/sessions/{sid}/case")
def case_check(sid: str, body: CaseIn, user: dict = Depends(sec.current_user)):
    if user["role"] not in ("ops", "admin"):
        raise HTTPException(403, "只有平台运营可以做带单核查")
    _own(sid, user)
    try:
        return cs.explain_case(sid, body.dispute_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- 板块信息
#
# 快捷问题为什么由服务端给，而不是前端写死：
# 写死的 chips 会出现在"点下去答不上来"的情况——知识库里根本没有这条，
# 用户点完直接掉进人工兜底，看起来像系统坏了。这里直接从本域知识库取高频问法，
# 保证点下去的每一条都能命中，而且改知识库不需要改页面。

# 每个板块放一条"不走普通检索"的示例，让第四层漏斗能被看见。
# 但三条的性质不同，不能都写成"发一句话"：
#   shop  买家说"转人工" → 真的转给店家客服
#   rule  商家说"我的店被扣分了" → 转给平台规则运营（助手没有账号数据）
#   gov   运营要"判这个案子" → 不是转人工，而是先选一单，再摊事实和条款
# 第三条以前写成「这个案子怎么判」，既是无指代的空话，又必然被 case_judge
# 规则拦成转人工，点下去永远像坏了。所以它是 kind=case，不是一句文本。
HANDOFF_CHIP = {
    "shop": {"q": "转人工", "kind": "handoff"},
    "rule": {"q": "我的店被扣分了", "kind": "handoff"},
    "gov": {"q": "挑一单查条款", "kind": "case", "category": "带单核查"},
}


def _chips(audience: str, scope: str, shop_id: int | None = None,
           limit: int = 5) -> list[dict]:
    """快捷问题必须来自**用户真正会命中的那份索引**。

    以前这里只按 audience 查库，店铺专属的一条都进不来：店老板自己录了
    「定制窗帘能退吗」，页面上却一条快捷入口都没有，只能靠买家自己敲字。
    现在走 faq.rows_from_db(audience, shop_id)，和检索用的是同一份合并结果，
    保证「点下去能答上来」这条约束在分店铺之后依然成立。
    """
    rows = faq.rows_from_db(audience, shop_id)
    # 同一分类最多取两条：全取"物流发货"会得到 6 个发货问题，
    # 看起来像只有一个板块。
    # 本店专属最多占 3 条：一家店有 5 条自己的话术就把它全铺满，买家看不到
    # "退款""发票"这些真正高频的问题，等于用一个窗口回答了十个问题里最小的那个。
    picked, seen, used = [], {}, set()
    own_n = 0
    OWN_CAP = 3
    for r in rows:
        q = r["question"]
        # 会被强制转人工规则截走的问题不能当快捷问题。
        # 知识库里确实有一条「转人工」，但只要文本里有"转人工"，
        # force_handoff 在检索之前就拦下了——点这个 chip 永远是"已转接人工"，
        # 用户会觉得知识库坏了。宁可不出这个 chip。
        if cs.force_handoff(q, scope)[0]:
            continue
        if q in used:
            continue
        if r.get("shop_id"):
            if own_n >= OWN_CAP:
                continue
            own_n += 1
        c = r["category"] or "通用"
        if seen.get(c, 0) >= 2:
            continue
        seen[c] = seen.get(c, 0) + 1
        used.add(q)
        picked.append({"q": q, "category": c, "own": bool(r.get("shop_id"))})
        if len(picked) >= limit:
            break
    return picked


def _board_shop(user: dict, shop_id: int | None) -> int | None:
    """board 用哪个店铺视野。商家只能是自己店；其他角色按传参，且必须是真店。"""
    if user["role"] == "merchant":
        return user["shop_id"]
    if not shop_id:
        return None
    rows = common.fetch_all("SELECT id FROM t_shop WHERE id=%s", (int(shop_id),))
    return int(rows[0]["id"]) if rows else None


def _board_ui(scope: str, role: str) -> dict:
    """这个板块的窗口长什么样、能做哪些动作。

    动作列表由服务端给，不再由前端按 scope 写 if/else。原因很具体：
    前端那份 if/else 漏了一个 else——治理板块落进了"平台介入 + 工作台"那一支，
    运营在治理窗口右上角看到了买家的「平台介入」，点下去是替别人投诉。
    只要动作定义还散在页面里，这种漏就一定还会发生第二次。
    """
    cfg = cs.SCOPES[scope]
    acts: list[dict] = []
    if scope == "shop":
        # 平台介入是买家的维权入口。商家点它是投诉自己，运营点它是替别人投诉。
        # 预览（?scope=shop）也不给：预览看到的是"买家会看到什么"，
        # 而买家只有一种角色。
        if role == "buyer":
            acts.append({"kind": "escalate", "label": "平台介入",
                         "title": "对处理不满意时可申请平台介入"})
        else:
            acts.append({"kind": "link", "label": "工作台", "href": "/service",
                         "title": "回到坐席工作台"})
    elif scope == "rule":
        acts += [{"kind": "link", "label": "纠纷中心", "href": "/dispute",
                  "title": "我的处罚、答辩与申诉都在这里"},
                 {"kind": "link", "label": "规则库", "href": "/rules"}]
    else:
        acts += [{"kind": "link", "label": "仲裁台", "href": "/platform",
                  "title": "受理投诉、裁决、处罚与复审"},
                 {"kind": "link", "label": "规则库", "href": "/rules"}]
    return {
        "avatar": cfg["avatar"], "badge": cfg["label"] if scope == "shop"
        else sec.ROLE_LABEL.get(role, role),
        "intro": cfg["intro"], "greet": cfg["greet"], "trace": cfg["trace"],
        "placeholder": cfg["placeholder"], "states": cfg["states"],
        "actions": acts,
        # 前端只认这个布尔值决定要不要画"申请平台介入"这条链路，
        # 不再自己判断角色。判断只有一处，检查也只有一处。
        "can_escalate": any(a["kind"] == "escalate" for a in acts),
    }


@router.get("/board")
def board(scope: str = "", shop_id: int | None = None,
          user: dict = Depends(sec.current_user)):
    """当前账号该进哪个咨询板块：标题、知识源、快捷问题、快捷入口。

    一个窗口服务三种角色，靠的就是这个接口——页面本身不再判断角色，
    判断逻辑在服务端，前端只负责渲染。

    scope 参数用于预览：商家想看自己店的客服长什么样、运营想走一遍买家流程，
    都通过 ?scope=shop 进来，权限在这一层校验，页面不用自己判断。
    shop_id 让店铺板块能报出「本店专属 N 条 / 平台通用 M 条」，
    否则店老板改了自己的话术，界面上一点变化都看不出来。
    """
    role = user["role"]
    want = (scope or "").strip()
    scope = want if (want in cs.SCOPES and want in ALLOWED_SCOPES.get(role, set())) \
        else DEFAULT_SCOPE.get(role, "shop")
    cfg = cs.SCOPES[scope]
    aud = cfg["audience"]
    shop = _board_shop(user, shop_id) if scope == "shop" else None

    rows = faq.rows_from_db(aud, shop)
    cats: dict[str, int] = {}
    for r in rows:
        cats[r["category"] or "通用"] = cats.get(r["category"] or "通用", 0) + 1
    chips = _chips(aud, scope, shop)
    hc = HANDOFF_CHIP.get(scope)
    if hc:
        chips.append({**hc, "category": hc.get("category", "转人工"),
                      "handoff": hc["kind"] == "handoff"})

    links = {
        "buyer": [{"label": "纠纷中心", "href": "/dispute"},
                  {"label": "规则库", "href": "/rules"}],
        "merchant": [{"label": "纠纷中心", "href": "/dispute"},
                     {"label": "规则库", "href": "/rules"},
                     {"label": "客服工作台", "href": "/service"}],
        "ops": [{"label": "仲裁台", "href": "/platform"},
                {"label": "纠纷中心", "href": "/dispute"},
                {"label": "规则库", "href": "/rules"}],
    }.get(role, [])

    shop_name = ""
    if shop:
        s = common.fetch_all("SELECT name FROM t_shop WHERE id=%s", (shop,))
        shop_name = s[0]["name"] if s else ""

    out = {
        "role": role, "role_label": sec.ROLE_LABEL.get(role, role),
        "scope": scope, "label": cfg["label"], "title": cfg["title"],
        "desc": cfg["desc"], "audience": aud,
        "audience_label": {"buyer": "店铺知识库", "merchant": "平台规则库",
                           "ops": "治理规范库"}[aud],
        # 知识构成：own 是本店/本域专属条目，global 是平台通用基线。
        # 检索时两者合并，本店同问法条目会覆盖平台那条（见 faq.rows_from_db）。
        "shop_id": shop, "shop_name": shop_name,
        "ui": _board_ui(scope, role),
        "chips": chips, "links": links,
        "stats": {"faqs": len(rows),
                  # own/global 报的是**库里各有多少条**，不是 dedupe 之后的条数：
                  # 本店有一条和平台同问法的话术时，检索会以本店为准（平台那条不进索引），
                  # 若按并集算，店老板会看到"平台通用 73 条"，和知识库里的 74 条对不上，
                  # 以为系统丢了一条。
                  "own": int(common.fetch_all(
                      "SELECT COUNT(*) AS n FROM t_faq WHERE audience=%s AND status='enabled' "
                      "AND shop_id=%s", (aud, shop))[0]["n"] or 0) if shop else 0,
                  "global": int(common.fetch_all(
                      "SELECT COUNT(*) AS n FROM t_faq WHERE audience=%s AND status='enabled' "
                      "AND shop_id IS NULL", (aud,))[0]["n"] or 0),
                  # effective = 这次提问真正参与检索的条数（去重后）
                  "effective": len(rows),
                  "categories": [{"name": k, "n": v}
                                 for k, v in sorted(cats.items(), key=lambda x: -x[1])]},
    }
    # 第三方接入：只有平台侧需要，它要看到三方各自卡在哪一步。
    if scope == "gov":
        out["gov"] = gov.board_stats()
    return out


# ---------------------------------------------------------------- 会话列表

@router.get("/sessions")
def list_sessions(status: str = "", q: str = "", scope: str = "shop",
                  limit: int = Query(40, le=100),
                  user: dict = Depends(sec.current_user)):
    """坐席队列。默认只看 scope=shop —— 商家问规则、运营问治理的会话
    混进来就是噪声：坐席点开一条"售假扣几分"的会话，能做的最多是把答案再念一遍。"""
    shop = _scope(user)
    where, args = ["1=1"], []
    if shop is not None:
        where.append("s.shop_id=%s")
        args.append(shop)
    if scope == "all":
        if user["role"] not in ("ops", "admin"):
            where.append("s.scope='shop'")
    elif scope in cs.SCOPES:
        where.append("s.scope=%s")
        args.append(scope)
    else:
        where.append("s.scope='shop'")
    if status in ("bot", "agent", "closed", "escalated"):
        where.append("s.status=%s")
        args.append(status)
    if q.strip():
        where.append("(s.buyer_name LIKE %s OR s.last_msg LIKE %s)")
        args += [f"%{q.strip()}%", f"%{q.strip()}%"]

    sql = f"""SELECT s.*, sh.name AS shop_name,
                     t.id AS ticket_id, t.status AS ticket_status, t.reason AS ticket_reason
              FROM t_cs_session s
              LEFT JOIN t_shop sh ON sh.id = s.shop_id
              LEFT JOIN t_ticket t ON t.id = (
                  SELECT MAX(id) FROM t_ticket WHERE session_id=s.id)
              WHERE {' AND '.join(where)}
              ORDER BY CASE s.status WHEN 'agent' THEN 0 WHEN 'bot' THEN 1 ELSE 2 END,
                       s.updated_at DESC LIMIT %s"""
    rows = common.fetch_all(sql, tuple(args + [limit]))
    return {"items": [{
        "id": r["id"], "buyer_name": r["buyer_name"], "channel": r["channel"],
        "status": r["status"], "scope": r.get("scope") or "shop",
        "shop_name": r.get("shop_name") or "",
        "handoff": bool(r["handoff"]),
        "last_msg": r.get("last_msg") or "", "msg_count": r["msg_count"] or 0,
        "ticket_id": r.get("ticket_id"), "ticket_status": r.get("ticket_status"),
        "ticket_reason": r.get("ticket_reason"),
        "updated_at": str(r.get("updated_at") or ""),
    } for r in rows]}


# ---------------------------------------------------------------- 坐席操作

class ReplyIn(BaseModel):
    text: str


@router.post("/sessions/{sid}/claim")
def claim(sid: str, user: dict = Depends(sec.require(*sec.AGENT_ROLES))):
    sess = _own(sid, user)
    try:
        return cs.claim(sess["id"], user)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/sessions/{sid}/reply")
def reply(sid: str, body: ReplyIn, user: dict = Depends(sec.require(*sec.AGENT_ROLES))):
    sess = _own(sid, user)
    try:
        return cs.agent_say(sess["id"], user, body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/sessions/{sid}/release")
def release(sid: str, user: dict = Depends(sec.require(*sec.AGENT_ROLES))):
    sess = _own(sid, user)
    return cs.release(sess["id"], user)


@router.post("/sessions/{sid}/distill")
def distill(sid: str, user: dict = Depends(sec.require(*sec.AGENT_ROLES))):
    """把这次人工回复沉淀进知识库，下次同类问题机器人就能自己答。"""
    sess = _own(sid, user)
    try:
        return cs.distill(sess["id"], user)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- 工单

@router.get("/tickets")
def tickets(status: str = "pending", limit: int = Query(50, le=200),
            user: dict = Depends(sec.require(*sec.AGENT_ROLES))):
    shop = _scope(user)
    where, args = ["1=1"], []
    if shop is not None:
        where.append("s.shop_id=%s")
        args.append(shop)
    if status in ("pending", "processing", "done"):
        where.append("t.status=%s")
        args.append(status)
    rows = common.fetch_all(
        f"""SELECT t.*, s.buyer_name, s.channel, s.status AS session_status
            FROM t_ticket t JOIN t_cs_session s ON s.id=t.session_id
            WHERE {' AND '.join(where)}
            ORDER BY t.priority DESC, t.id ASC LIMIT %s""",
        tuple(args + [limit]))
    reason_label = {"user_request": "买家要求人工", "complaint": "投诉维权",
                    "fraud": "涉假涉诈", "compensation": "赔付诉求", "abuse": "情绪激烈",
                    "low_conf": "知识库无答案", "llm_unavailable": "模型未配置",
                    "llm_uncertain": "模型无把握", "llm_handoff": "模型建议转人工",
                    "shop_suspended": "店铺被平台停业",
                    "case_lookup": "要查本店个案数据", "case_judge": "要求判定在办案件",
                    "legal": "涉诉讼/监管投诉"}
    return {"items": [{
        "id": r["id"], "session_id": r["session_id"], "buyer_name": r["buyer_name"],
        "channel": r["channel"], "reason": r["reason"],
        "reason_label": reason_label.get(r["reason"], r["reason"]),
        "question": r.get("question") or "", "bot_reply": r.get("bot_reply") or "",
        "status": r["status"], "priority": r["priority"], "agent_reply": r.get("agent_reply"),
        "created_at": str(r.get("created_at") or ""),
        "session_status": r.get("session_status"),
    } for r in rows]}


# ---------------------------------------------------------------- 知识库

class FaqIn(BaseModel):
    question: str
    answer: str
    category: str | None = "通用"
    audience: str | None = None
    shop_id: int | None = None
    cat_scope: str | None = ""
    keywords: str | None = ""
    aliases: str | None = ""
    priority: int | None = 0


class FaqPatch(BaseModel):
    question: str | None = None
    answer: str | None = None
    category: str | None = None
    audience: str | None = None
    shop_id: int | None = None
    cat_scope: str | None = None
    keywords: str | None = None
    aliases: str | None = None
    priority: int | None = None
    status: str | None = None


class ProbeIn(BaseModel):
    query: str
    audience: str | None = "buyer"
    shop_id: int | None = None
    top_k: int | None = 5


# 谁能维护哪个知识域、哪些条目。三档权限，不是一句话能盖住的：
#   * 商家：只能维护「买家域 + 本店」的条目。平台通用口径他改不了（改了全平台都跟着变），
#           平台规则库/治理规范库更碰不到——那是平台资产的底线。
#   * 运营/管理员：可以维护三个域，也可以把买家域条目的归属指定到某家店。
#   * 买家：没有任何写权限。以前默认落到 {"buyer"}，等于谁都能往店铺话术里塞一条
#           「本店支持无条件退换」，这不是知识管理，是给自己开后门。
def _write_ctx(user: dict) -> tuple[set[str], int | None, bool]:
    """返回 (可写知识域, 强制的店铺归属, 是否可指定店铺归属)。"""
    role = user["role"]
    if role == "merchant":
        return {"buyer"}, user["shop_id"], False
    if role in ("ops", "admin"):
        return set(faq.AUDIENCES), None, True
    return set(), None, False


def _shop_name_map() -> dict[int, str]:
    return {int(r["id"]): r["name"] for r in common.fetch_all("SELECT id, name FROM t_shop")}


def _resolve_shop(body_shop: int | None, aud: str,
                  force: int | None, can_pick: bool) -> int | None:
    """知识条目的归属店铺。规则域/治理域永远不挂店——它们回答的是平台统一口径。"""
    if aud != "buyer":
        return None
    if not can_pick:
        return force
    if body_shop is None:
        return None
    if not common.fetch_all("SELECT 1 AS x FROM t_shop WHERE id=%s", (int(body_shop),)):
        raise HTTPException(404, "店铺不存在")
    return int(body_shop)


def _can_touch(row: dict, allowed: set[str], force: int | None, can_pick: bool) -> bool:
    """这一条我能不能改。归属与知识域都要过。"""
    if (row.get("audience") or "buyer") not in allowed:
        return False
    if can_pick:
        return True
    sid = row.get("shop_id")
    # 平台通用条目对商家是只读的：正确做法是"复制为本店口径"，不是直接改全局。
    return sid is not None and int(sid) == int(force or 0)


@router.get("/faq")
def faq_list(q: str = "", category: str = "", audience: str = "buyer",
             shop_id: int | None = None, scope: str = "",
             limit: int = Query(100, le=300),
             user: dict = Depends(sec.current_user)):
    """知识库列表。

    scope 三态决定看到什么：
      ""（默认）→ 平台通用 + 指定店铺（商家看到的是「我能用的全部话术」）
      global    → 只看平台通用基线
      own       → 只看本店专属
    每条都带 editable，前端据此决定给不给"编辑"按钮——让用户点出一个 403
    是最差的知识库交互。
    """
    aud = audience if audience in faq.AUDIENCES else "buyer"
    allowed, force, can_pick = _write_ctx(user)
    role = user["role"]
    if role in ("merchant", "buyer"):
        aud = "buyer"
    eff_shop = force if not can_pick else shop_id

    where, args = ["audience=%s"], [aud]
    if scope == "global":
        where.append("shop_id IS NULL")
    elif scope == "own":
        if eff_shop is None:
            where.append("1=0")            # 没有店铺归属的人，"本店专属"就是空集
        else:
            where.append("shop_id=%s")
            args.append(int(eff_shop))
    elif eff_shop is not None:
        where.append("(shop_id IS NULL OR shop_id=%s)")
        args.append(int(eff_shop))
    else:
        where.append("shop_id IS NULL")
    if q.strip():
        where.append("(question LIKE %s OR keywords LIKE %s OR aliases LIKE %s)")
        args += [f"%{q.strip()}%"] * 3
    if category.strip():
        where.append("category=%s")
        args.append(category.strip())
    rows = common.fetch_all(
        f"""SELECT id, question, answer, category, audience, shop_id, cat_scope,
                   keywords, aliases, priority, status, hit_count, source, updated_at
            FROM t_faq WHERE {' AND '.join(where)}
            ORDER BY (CASE WHEN shop_id IS NOT NULL THEN 2
                           WHEN cat_scope<>'' THEN 1 ELSE 0 END) DESC,
                     priority DESC, hit_count DESC, id ASC LIMIT %s""",
        tuple(args + [limit]))

    names = _shop_name_map()
    items = []
    for r in rows:
        sid = r.get("shop_id")
        items.append({**r, "shop_id": int(sid) if sid is not None else None,
                      "cat_scope": r.get("cat_scope") or "",
                      "shop_name": names.get(int(sid), "") if sid is not None else "",
                      "own": sid is not None,
                      "editable": _can_touch(r, allowed, force, can_pick),
                      "updated_at": str(r.get("updated_at") or "")})

    cats = common.fetch_all(
        "SELECT category, COUNT(*) AS n FROM t_faq WHERE status='enabled' AND audience=%s "
        + ("AND (shop_id IS NULL OR shop_id=%s) " if eff_shop is not None
           else "AND shop_id IS NULL ")
        + "GROUP BY category",
        tuple([aud] + ([int(eff_shop)] if eff_shop is not None else [])))
    sizes = {r["audience"]: r["n"] for r in common.fetch_all(
        "SELECT audience, COUNT(*) AS n FROM t_faq WHERE status='enabled' GROUP BY audience")}
    return {"items": items, "audience": aud, "sizes": sizes,
            "shop_id": eff_shop, "can_pick_shop": can_pick,
            "shops": [{"id": i, "name": n} for i, n in sorted(names.items())],
            "categories": [{"name": c["category"], "n": c["n"]} for c in cats],
            "threshold": faq.THRESHOLD, "version": faq.version()}


@router.post("/faq")
def faq_create(body: FaqIn, user: dict = Depends(sec.current_user)):
    if not body.question.strip() or not body.answer.strip():
        raise HTTPException(400, "问法和答案都不能为空")
    allowed, force, can_pick = _write_ctx(user)
    if not allowed:
        raise HTTPException(403, "该角色没有知识库维护权限")
    aud = body.audience if body.audience in faq.AUDIENCES else "buyer"
    if aud not in allowed:
        raise HTTPException(403, "当前角色不能维护该知识域")
    shop = _resolve_shop(body.shop_id, aud, force, can_pick)
    ts = schema.now()
    # 类目模板：shop_id 为空 + cat_scope 非空 = 对该品类所有店生效的基线。
    # 只有 ops/admin 能建（商家给自己店建专属话术，不能给整个品类定口径）。
    cat_scope = (body.cat_scope or "").strip()
    if cat_scope and user["role"] not in ("ops", "admin"):
        raise HTTPException(403, "只有平台运营可以维护类目模板")
    if cat_scope and shop is not None:
        raise HTTPException(400, "类目模板是对整个品类的基线，不能同时指定具体店铺")
    if cat_scope and aud != "buyer":
        raise HTTPException(400, "类目模板只适用于买家域（店铺话术），规则域/治理域是平台统一口径")
    # 审核流：运营/管理员手动建条目直接生效（他们有审核权）；
    # 商家建本店口径则先 pending，等平台过审。否则「新建条目」就成了
    # 商家绕过沉淀审核、往全店话术里塞内容的另一条后门。
    status = "enabled" if user["role"] in ("ops", "admin") else "pending"
    fid = common.insert(
        """INSERT INTO t_faq (question, answer, category, audience, shop_id, cat_scope,
               keywords, aliases, priority, status, hit_count, source, created_by,
               created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'manual',%s,%s,%s)""",
        (body.question.strip()[:250], body.answer.strip(), (body.category or "通用")[:30],
         aud, shop, cat_scope, (body.keywords or "")[:250], (body.aliases or "")[:490],
         body.priority or 0, status, user["id"], ts, ts))
    if status == "enabled":
        faq.reload()
    return {"ok": True, "id": fid, "audience": aud, "shop_id": shop,
            "cat_scope": cat_scope, "status": status, "version": faq.version()}


@router.patch("/faq/{fid}")
def faq_update(fid: int, body: FaqPatch, user: dict = Depends(sec.current_user)):
    rows = common.fetch_all("SELECT * FROM t_faq WHERE id=%s", (fid,))
    if not rows:
        raise HTTPException(404, "知识条目不存在")
    cur = rows[0]
    allowed, force, can_pick = _write_ctx(user)
    if not _can_touch(cur, allowed, force, can_pick):
        if (cur.get("audience") or "buyer") not in allowed:
            raise HTTPException(403, "当前角色不能维护该知识域")
        raise HTTPException(403, "平台通用条目不能直接修改；"
                                "请「复制为本店口径」，或联系平台运营调整基线")
    if body.audience and body.audience not in allowed:
        raise HTTPException(403, "当前角色不能把条目移到该知识域")
    # 状态字段（pending/enabled/rejected/disabled）是审核流的一部分：
    # 只有 ops/admin 能改，否则商家把一条 pending 直接 PATCH 成 enabled 就绕过了审核。
    if body.status is not None and user["role"] not in ("ops", "admin"):
        raise HTTPException(403, "只有平台运营可以改动条目的审核状态（待审/生效/驳回）")
    # 类目模板同样只有 ops/admin 能设；且模板不挂具体店铺。
    if body.cat_scope is not None and user["role"] not in ("ops", "admin"):
        raise HTTPException(403, "只有平台运营可以维护类目模板")
    # 只更新**真的传了**的字段。以前这里写成 (body.x or "")，
    # 结果 PATCH 只改 answer 时 question 变成空串，直接被下面的校验打成 400；
    # 更隐蔽的是 keywords/aliases 会被静默清空——运营改个答案，关键词全没了。
    fields = {
        "question": body.question.strip()[:250] if body.question is not None else None,
        "answer": body.answer.strip() if body.answer is not None else None,
        "category": (body.category or "")[:30] if body.category is not None else None,
        "audience": body.audience,
        "cat_scope": (body.cat_scope or "").strip() if body.cat_scope is not None else None,
        "keywords": body.keywords[:250] if body.keywords is not None else None,
        "aliases": body.aliases[:490] if body.aliases is not None else None,
        "priority": body.priority,
        "status": body.status,
    }
    # 归属变更要单独算：改了知识域（buyer→merchant）就必须把 shop_id 清空，
    # 否则规则库里会挂着一条"只对某家店生效"的条目，永远不会被检索到。
    # 清空是 NULL，不是空串——下面按"值非 None 才更新"的循环天然会跳过 NULL，
    # 所以这类赋值必须走独立的 SQL 片段，不能被 None 过滤掉。
    new_aud = body.audience or (cur.get("audience") or "buyer")
    extra: list[str] = []
    extra_args: list = []
    # 类目模板是对整个品类的基线，不能同时挂某家店。
    # 一旦设了 cat_scope（非空），shop_id 必须清空，否则这条永远检索不到。
    if body.cat_scope is not None:
        if (body.cat_scope or "").strip():
            extra.append("shop_id=NULL")
        elif body.shop_id is not None:
            # 清空 cat_scope 的同时显式指定了 shop_id，走下面的正常归属分支
            pass
    if body.shop_id is not None or new_aud != (cur.get("audience") or "buyer"):
        if body.shop_id is not None and not can_pick:
            raise HTTPException(403, "只有平台运营可以指定知识条目的店铺归属")
        target = _resolve_shop(
            body.shop_id if body.shop_id is not None else cur.get("shop_id"),
            new_aud, force, can_pick)
        if target is None:
            extra.append("shop_id=NULL")
        else:
            extra.append("shop_id=%s")
            extra_args.append(target)
    sets, args = list(extra), list(extra_args)
    for k, v in fields.items():
        if v is None:
            continue
        if k in ("question", "answer") and not v:
            raise HTTPException(400, "问法和答案不能为空")
        sets.append(f"{k}=%s")
        args.append(v)
    if not sets:
        return {"ok": True, "changed": 0}
    sets.append("updated_at=%s")
    args += [schema.now(), fid]
    common.execute(f"UPDATE t_faq SET {', '.join(sets)} WHERE id=%s", tuple(args))
    faq.reload()          # 改完必须重建索引 + 提版本号，否则旧答案会继续被缓存命中
    return {"ok": True, "changed": len(sets) - 1, "version": faq.version()}


@router.delete("/faq/{fid}")
def faq_delete(fid: int, user: dict = Depends(sec.require("admin", "ops", "merchant"))):
    """下架而不是物理删除：删掉之后历史会话里的引用就断了，
    而且没法回答"这条为什么没了"。status=disabled 保留可追溯性。"""
    rows = common.fetch_all("SELECT id, audience, shop_id FROM t_faq WHERE id=%s", (fid,))
    if not rows:
        raise HTTPException(404, "知识条目不存在")
    allowed, force, can_pick = _write_ctx(user)
    if not _can_touch(rows[0], allowed, force, can_pick):
        raise HTTPException(403, "当前角色不能下架该条目（平台通用条目请联系平台运营）")
    common.execute("UPDATE t_faq SET status='disabled', updated_at=%s WHERE id=%s",
                   (schema.now(), fid))
    faq.reload()
    return {"ok": True, "version": faq.version()}


# ---------------------------------------------------------------- 沉淀审核

@router.get("/faq/pending")
def faq_pending(user: dict = Depends(sec.require("admin", "ops"))):
    """待审队列。人工沉淀先 pending，这里过审才 enabled 进机器人。"""
    items = cs.pending_faqs()
    return {"items": items, "total": len(items)}


class ReviewIn(BaseModel):
    approve: bool
    note: str = ""


@router.post("/faq/{fid}/review")
def faq_review(fid: int, body: ReviewIn, user: dict = Depends(sec.require("admin", "ops"))):
    """审核通过 / 驳回。通过才 reload 进检索；驳回转 rejected 保留可追溯。"""
    try:
        return cs.review_faq(fid, body.approve, user, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/faq/probe")
def faq_probe(body: ProbeIn, user: dict = Depends(sec.current_user)):
    """阈值调试器：把一句话的完整打分过程摊开。

    没有这个接口，运营永远不理解"为什么这句话不走知识库"，
    只能凭感觉反复改阈值——而改阈值是最容易把系统改坏的操作。

    分店铺之后它更重要了：同一个问题在 A 店命中、B 店不命中是**正常**的，
    没有这个工具，运营只会以为知识库坏了。返回里带上命中的条目归属。
    """
    aud = body.audience if body.audience in faq.AUDIENCES else "buyer"
    _allowed, force, can_pick = _write_ctx(user)
    # 商家/买家只能调试自己店（买家为平台通用）；运营可以指定任意店
    shop = body.shop_id if can_pick else force
    if aud != "buyer":
        shop = None
    nq = faq.norm(body.query)
    idx = faq.index(aud, shop)
    cands = idx.search(body.query, top_k=max(1, min(body.top_k or 5, 10)))
    best, _ = idx.best(body.query)
    return {
        "query": body.query, "audience": aud, "shop_id": shop,
        "normalized": nq,
        "features": faq.feats(nq),
        "threshold": faq.THRESHOLD,
        "hit": best is not None,
        "candidates": [{
            "id": c["id"], "question": c["question"], "answer": c["answer"][:160],
            "sim": c["sim"], "cov": c["cov"], "cov_idf": c["cov_idf"],
            "cov_cnt": c["cov_cnt"], "sat": c["sat"], "matched": c["matched"],
            "shop_id": c.get("shop_id"), "own": bool(c.get("shop_id")),
        } for c in cands],
        "version": faq.version(),
    }


# ---------------------------------------------------------------- 指标

@router.get("/metrics")
def metrics(user: dict = Depends(sec.current_user)):
    m = cs.metrics(_scope(user))
    return m


@router.get("/categories")
def categories(audience: str = "buyer", user: dict = Depends(sec.current_user)):
    aud = audience if audience in faq.AUDIENCES else "buyer"
    rows = common.fetch_all(
        "SELECT category, COUNT(*) AS n FROM t_faq WHERE status='enabled' AND audience=%s "
        "GROUP BY category ORDER BY n DESC", (aud,))
    return {"items": rows}


# ---------------------------------------------------------------- 启动引导

def boot() -> None:
    """启动时把「三个知识域 × 每家店」的索引都建好。

    只建一个域，另外两个板块第一句提问会退化成空索引——表现出来就是
    "知识库明明有 20 条，却说没有依据"。
    只建平台通用索引，店家自己录的话术第一句提问也永远命不中：
    第一次落到 load() 里现场建索引要几百毫秒，用户看到的是"这条问题特别慢"。
    """
    shops = faq.shop_ids()
    for a in faq.AUDIENCES:
        faq.load(a, None)
        for sid in shops:
            faq.load(a, sid)
