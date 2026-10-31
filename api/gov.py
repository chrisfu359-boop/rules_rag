"""平台监管接口层：纠纷、处罚、申诉、店铺监管、看板。

三类调用方，权限模型和客服模块保持一致（越权返回 404 而不是 403）：

  * 买家      只能看自己发起的纠纷，能补充材料、申诉裁决
  * 商家      只能看"针对本店"和"自己发起"的纠纷，能答辩、申诉处罚
  * 平台      ops/admin 看全量，能受理、裁决、处罚、终审申诉

裁决与处罚接口只对 ops/admin 开放。商家能申诉但绝对不能自己裁决——
这不是 UI 藏按钮能解决的，必须在服务端拦。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import common
import service.gov as gov
from api import schema, security as sec

router = APIRouter(prefix="/api/gov", tags=["gov"])

PLATFORM = ("ops", "admin")
# 分工：平台运营（ops）负责裁决；系统管理员（admin）能查看但不能替仲裁下裁决
# （与 docs/18 的设计一致：管理员改模型配置，不参与业务裁决）。
ARBITER = ("ops",)


# ---------------------------------------------------------------- 工具

def _is_platform(user: dict) -> bool:
    return user["role"] in PLATFORM


def _can_view(d: dict, user: dict) -> bool:
    if _is_platform(user):
        return True
    if d.get("shop_id") and d["shop_id"] == user.get("shop_id"):
        return True
    return d.get("buyer_id") == user["id"]


def _own(did: int, user: dict) -> dict:
    d = gov.get(did)
    if not d:
        raise HTTPException(404, "纠纷不存在")
    if not _can_view(d, user):
        raise HTTPException(404, "纠纷不存在")
    return d


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


def _row(d: dict) -> dict:
    return {
        "id": d["id"], "dispute_no": d["dispute_no"], "shop_id": d["shop_id"],
        "buyer_name": d.get("buyer_name") or "", "dtype": d["dtype"],
        "type_label": gov.DTYPES.get(d["dtype"], {}).get("label", d["dtype"]),
        "title": d["title"], "claim": d.get("claim") or "",
        "claim_amount": float(d.get("claim_amount") or 0),
        "status": d["status"], "stage": d["stage"],
        "liability": d.get("liability") or "",
        "ai_confidence": float(d.get("ai_confidence") or 0),
        "ai_remedy": d.get("ai_remedy") or "",
        "remedy_label": gov.REMEDY_LABEL.get(d.get("ai_remedy") or "", ""),
        "ai_points": float(d.get("ai_points") or 0),
        "ai_basis": d.get("ai_basis") or "",
        "ai_reasoning": d.get("ai_reasoning") or "",
        "priority": d.get("priority") or 0,
        "sla_due": str(d.get("sla_due") or ""),
        "created_at": str(d.get("created_at") or ""),
        "updated_at": str(d.get("updated_at") or ""),
        "closed_at": str(d.get("closed_at") or ""),
        "order_no": d.get("order_no") or "",
        "session_id": d.get("session_id") or "",
    }


# ---------------------------------------------------------------- 元数据

@router.get("/orders")
def shop_orders(shop_id: int, user: dict = Depends(sec.current_user)):
    """投诉表单用：这家店可选的订单（按付款时间倒序）。

    订单号是仲裁的事实锚点。让买家手打 18 位单号，实际上等于放弃这个锚点——
    没人愿意翻订单再抄一遍，最后所有案子都变成「无订单号，只能依据陈述」。

    带商品名是必须的：只给一串订单号和金额，当事人自己都认不出是哪一单，
    选错单号比不选更糟——平台会拿错误的订单去判时效。

    归属校验：商家只能查自己店的订单；运营/管理员是平台侧、可查全平台；
    买家需要它来发起纠纷（订单未绑买家，属已知限制，见 docs/17）。
    """
    if user["role"] == "merchant" and user.get("shop_id") != shop_id:
        raise HTTPException(404, "店铺不存在")   # 越权返 404，不暴露店铺是否存在
    shop = common.fetch_all("SELECT name FROM t_shop WHERE id=%s", (shop_id,))
    if not shop:
        raise HTTPException(404, "店铺不存在")
    rows = common.fetch_all(
        "SELECT order_no, amount, status, category, paid_at, received_at, "
        "item_title, sku, qty, tracking_no FROM t_order "
        "WHERE shop_name=%s ORDER BY paid_at DESC LIMIT 10", (shop[0]["name"],))
    return {"items": [{
        "order_no": r["order_no"], "amount": float(r.get("amount") or 0),
        "status": r.get("status") or "", "category": r.get("category") or "",
        "paid_at": str(r.get("paid_at") or ""), "received_at": str(r.get("received_at") or ""),
        "item_title": r.get("item_title") or "", "sku": r.get("sku") or "",
        "qty": int(r.get("qty") or 1), "tracking_no": r.get("tracking_no") or "",
    } for r in rows]}


@router.get("/meta")
def meta(user: dict = Depends(sec.current_user)):
    """前端需要的所有枚举与阈值一次给全，避免把"虚假宣传扣 2 分"这种规则
    写死在页面上——规则改了还得改前端，迟早两边对不上。"""
    shops = common.fetch_all(
        "SELECT s.id, s.name, s.platform, c.status, c.credit_score, c.level "
        "FROM t_shop s LEFT JOIN t_shop_credit c ON c.shop_id=s.id ORDER BY s.id")
    return {
        "types": [{"key": k, "label": v["label"], "desc": v["desc"],
                   "severity": v["severity"], "auto": v["auto"],
                   "points": v["points"], "clause": v["clause"],
                   "doc": v["doc"], "need": v["need"],
                   "remedy": gov.REMEDY_LABEL.get(v["remedy"], v["remedy"])}
                  for k, v in gov.DTYPES.items()],
        "remedies": gov.REMEDY_LABEL,
        "penalty_types": gov.PENALTY_LABEL,
        "evidence_types": gov.EVIDENCE_TYPES,
        "status_label": gov.STATUS_LABEL,
        "nodes": [{"points": n, "type": t, "label": lb, "why": why} for n, t, lb, why in gov.NODES],
        "shops": [{"id": s["id"], "name": s["name"], "platform": s["platform"],
                   "status": s["status"] or "normal",
                   "status_label": gov.STATUS_LABEL.get(s["status"] or "normal", ""),
                   "credit_score": s["credit_score"] or 100,
                   "level": s["level"] or "良好",
                   "self": s["id"] == user.get("shop_id")} for s in shops],
        "threshold": gov.AUTO_CONF,
        "max_amount": gov.AUTO_MAX_AMOUNT,
        "appeal_days": gov.APPEAL_DAYS,
        "sla_hours": gov.SLA_HOURS,
    }


@router.get("/clauses")
def clauses(q: str = "", limit: int = Query(20, le=50),
            user: dict = Depends(sec.current_user)):
    """按条款号或关键词查依据。仲裁员填条款号时用它核对，
    填错一个不存在的小节号，整份裁决书就作废了。"""
    args, where = [], "1=1"
    if q.strip():
        where = "(c.clause_no LIKE %s OR c.content LIKE %s)"
        args = [f"%{q.strip()}%", f"%{q.strip()}%"]
    rows = common.fetch_all(
        f"""SELECT c.id, c.clause_no, c.content, c.clause_path, d.doc_name
            FROM t_clause c JOIN t_rule_doc d ON d.id=c.doc_id
            WHERE {where} AND d.doc_name IN
                ('商家违规行为管理规则','商家售后服务管理规范','网络交易监督管理办法',
                 '电子商务法','消费者权益保护法','网络购买商品七日无理由退货暂行办法')
            ORDER BY CASE d.doc_name WHEN '商家违规行为管理规则' THEN 0
                     WHEN '商家售后服务管理规范' THEN 1 ELSE 2 END, c.order_index
            LIMIT {int(limit)}""", tuple(args))
    return {"items": [{"clause_no": r["clause_no"], "doc_name": r["doc_name"],
                       "content": (r["content"] or "")[:300],
                       "path": r.get("clause_path") or ""} for r in rows]}


# ---------------------------------------------------------------- 纠纷：提交

class Evidence(BaseModel):
    k: str
    v: str | None = ""


class NewDispute(BaseModel):
    shop_id: int
    dtype: str
    title: str
    claim: str | None = ""
    claim_amount: float | None = 0
    claim_type: str | None = "refund"
    evidence: list[Evidence] | None = None
    session_id: str | None = None
    order_no: str | None = ""


@router.post("/disputes")
def create_dispute(body: NewDispute, user: dict = Depends(sec.current_user)):
    ev = [{"k": e.k[:30], "v": (e.v or "")[:300]} for e in (body.evidence or [])]
    try:
        return gov.create(body.shop_id, body.dtype, body.title, body.claim or "",
                          float(body.claim_amount or 0), ev, user,
                          body.session_id, body.order_no or "",
                          body.claim_type or "refund")
    except ValueError as e:
        raise _bad(e)


# ---------------------------------------------------------------- 纠纷：列表

@router.get("/disputes")
def list_disputes(status: str = "", dtype: str = "", q: str = "",
                  role: str = "", limit: int = Query(50, le=200),
                  user: dict = Depends(sec.current_user)):
    where, args = ["1=1"], []
    if not _is_platform(user):
        where.append("(d.shop_id=%s OR d.buyer_id=%s)")
        args += [user.get("shop_id") or -1, user["id"]]
    if role == "buyer":
        where.append("d.buyer_id=%s")
        args.append(user["id"])
    elif role == "shop" and user.get("shop_id"):
        where.append("d.shop_id=%s")
        args.append(user["shop_id"])
    if status:
        where.append("d.status=%s")
        args.append(status)
    if dtype:
        where.append("d.dtype=%s")
        args.append(dtype)
    if q.strip():
        where.append("(d.title LIKE %s OR d.dispute_no LIKE %s OR d.buyer_name LIKE %s)")
        args += [f"%{q.strip()}%"] * 3

    rows = common.fetch_all(
        f"""SELECT d.*, s.name AS shop_name FROM t_dispute d
            LEFT JOIN t_shop s ON s.id=d.shop_id
            WHERE {' AND '.join(where)}
            ORDER BY CASE d.status WHEN 'pending_human' THEN 0 WHEN 'arbitrating' THEN 1
                                   WHEN 'resolved' THEN 2 ELSE 3 END,
                     d.priority DESC, d.id DESC LIMIT %s""",
        tuple(args + [limit]))
    return {"items": [{**_row(r), "shop_name": r.get("shop_name") or ""} for r in rows],
            "counts": {r["status"]: r["n"] for r in common.fetch_all(
                "SELECT status, COUNT(*) AS n FROM t_dispute GROUP BY status")}}


@router.get("/disputes/{did}")
def dispute_detail(did: int, user: dict = Depends(sec.current_user)):
    _own(did, user)
    d = gov.detail(did)
    d["dispute"]["claim_amount"] = float(d["dispute"].get("claim_amount") or 0)
    d["can_arbitrate"] = user["role"] in ARBITER
    d["can_appeal"] = d["dispute"]["status"] == "resolved"
    return d


# ---------------------------------------------------------------- 纠纷：流转

class SayIn(BaseModel):
    text: str


@router.post("/disputes/{did}/messages")
def say(did: int, body: SayIn, user: dict = Depends(sec.current_user)):
    d = _own(did, user)
    if _is_platform(user):
        raise HTTPException(400, "平台裁决请使用裁决接口，不要在当事人对话里发言")
    # 角色由服务端判定，不由前端传：商家说自己是在"买家补充材料"，
    # 就能绕开"商家答辩"的职责边界。
    as_role = "merchant" if (user.get("shop_id") and d["shop_id"] == user["shop_id"]
                             and d.get("buyer_id") != user["id"]) else "buyer"
    try:
        return gov.reply(did, user, body.text, as_role)
    except ValueError as e:
        raise _bad(e)


@router.post("/disputes/{did}/claim")
def claim(did: int, user: dict = Depends(sec.require(*ARBITER))):
    try:
        return gov.claim(did, user)
    except ValueError as e:
        raise _bad(e)


class VerdictIn(BaseModel):
    liability: str
    remedy: str
    amount: float | None = 0
    points: float | None = 0
    clause: str | None = ""
    note: str
    ptype: str | None = ""
    days: int | None = None


@router.post("/disputes/{did}/verdict")
def do_verdict(did: int, body: VerdictIn, user: dict = Depends(sec.require(*ARBITER))):
    if body.liability not in ("merchant", "buyer", "shared", "insufficient"):
        raise HTTPException(400, "责任方取值非法")
    if body.remedy not in gov.REMEDY_LABEL:
        raise HTTPException(400, "处理方案取值非法")
    try:
        return gov.verdict(did, user, body.liability, body.remedy,
                           body.amount or 0, body.points or 0, body.clause or "",
                           body.note, body.ptype or "", body.days)
    except ValueError as e:
        raise _bad(e)


class CloseIn(BaseModel):
    reason: str | None = "买家撤回投诉"


@router.post("/disputes/{did}/close")
def close(did: int, body: CloseIn, user: dict = Depends(sec.current_user)):
    d = _own(did, user)
    if not (_is_platform(user) or d.get("buyer_id") == user["id"]):
        raise HTTPException(403, "只有买家本人或平台可以关闭纠纷")
    try:
        return gov.close(did, user, body.reason or "买家撤回投诉")
    except ValueError as e:
        raise _bad(e)


# ---------------------------------------------------------------- 处罚

@router.get("/penalties")
def penalties(shop_id: int | None = None, status: str = "",
              limit: int = Query(80, le=200), user: dict = Depends(sec.current_user)):
    where, args = ["1=1"], []
    if not _is_platform(user):
        where.append("p.shop_id=%s")
        args.append(user.get("shop_id") or -1)
    elif shop_id:
        where.append("p.shop_id=%s")
        args.append(shop_id)
    if status:
        where.append("p.status=%s")
        args.append(status)
    rows = common.fetch_all(
        f"""SELECT p.*, s.name AS shop_name,
                   (SELECT COUNT(*) FROM t_appeal a
                     WHERE a.target_type='penalty' AND a.target_id=p.id) AS appeals
            FROM t_penalty p LEFT JOIN t_shop s ON s.id=p.shop_id
            WHERE {' AND '.join(where)}
            ORDER BY p.id DESC LIMIT %s""", tuple(args + [limit]))
    return {"items": [{
        "id": r["id"], "shop_id": r["shop_id"], "shop_name": r.get("shop_name") or "",
        "dispute_id": r.get("dispute_id"), "ptype": r["ptype"],
        "type_label": gov.PENALTY_LABEL.get(r["ptype"], r["ptype"]),
        "points": float(r.get("points") or 0), "node": r.get("node") or "",
        "reason": r.get("reason") or "", "basis": r.get("basis") or "",
        "clause_no": r.get("clause_no") or "", "status": r["status"],
        "auto": bool(r.get("auto")), "appeals": int(r.get("appeals") or 0),
        "effective_at": str(r.get("effective_at") or ""),
        "expire_at": str(r.get("expire_at") or ""),
        "created_at": str(r.get("created_at") or ""),
    } for r in rows]}


class RevokeIn(BaseModel):
    reason: str


@router.post("/penalties/expire")
def penalties_expire(user: dict = Depends(sec.require(*PLATFORM))):
    """手动触发罚单到期回收。定时任务之外留一个手动入口：
    演示时要证明「停业 7 天后自动恢复」不能真等 7 天，得能当场结算。"""
    r = gov.expire_due()
    if r["expired"]:
        schema.audit(user["id"], "penalty_expire_manual",
                     ",".join(str(s) for s in r["shops"]), "",
                     f"罚单 {r['expired']} 张，恢复 {len(r['recovered'])} 家")
    return r


@router.post("/penalties/{pid}/revoke")
def revoke(pid: int, body: RevokeIn, user: dict = Depends(sec.require(*PLATFORM))):
    try:
        r = gov.revoke_penalty(pid, user["id"], body.reason)
        schema.audit(user["id"], "penalty_revoke", str(pid), "", body.reason)
        return r
    except ValueError as e:
        raise _bad(e)


class PenaltyIn(BaseModel):
    shop_id: int
    ptype: str
    days: int | None = None
    reason: str
    dispute_id: int | None = None


@router.post("/penalties")
def add_penalty(body: PenaltyIn, user: dict = Depends(sec.require(*PLATFORM))):
    if body.ptype not in gov.PENALTY_LABEL or body.ptype == "record":
        raise HTTPException(400, "处罚类型非法")
    if not body.reason.strip():
        raise HTTPException(400, "必须填写处罚理由")
    try:
        r = gov.manual_penalty(body.shop_id, body.ptype, body.days,
                               body.reason, user["id"], body.dispute_id)
        schema.audit(user["id"], "penalty_create", f"shop:{body.shop_id}", "",
                     f"{body.ptype} {body.days or ''} {body.reason}")
        return r
    except ValueError as e:
        raise _bad(e)


# ---------------------------------------------------------------- 申诉

class AppealIn(BaseModel):
    target_type: str
    target_id: int
    reason: str
    evidence: list[Evidence] | None = None


@router.post("/appeals")
def appeal(body: AppealIn, user: dict = Depends(sec.current_user)):
    ev = [{"k": e.k[:30], "v": (e.v or "")[:300]} for e in (body.evidence or [])]
    try:
        r = gov.appeal_create(body.target_type, body.target_id, user,
                              body.reason, ev)
        schema.audit(user["id"], "appeal_create",
                     f"{body.target_type}:{body.target_id}", "", body.reason[:160])
        return r
    except ValueError as e:
        raise _bad(e)


@router.get("/appeals")
def appeals(status: str = "", limit: int = Query(60, le=200),
            user: dict = Depends(sec.current_user)):
    where, args = ["1=1"], []
    if not _is_platform(user):
        where.append("(a.appellant_id=%s OR a.shop_id=%s)")
        args += [user["id"], user.get("shop_id") or -1]
    if status:
        where.append("a.status=%s")
        args.append(status)
    rows = common.fetch_all(
        f"""SELECT a.*, s.name AS shop_name, u.display_name AS appellant_name,
                   d.dispute_no
            FROM t_appeal a
            LEFT JOIN t_shop s ON s.id=a.shop_id
            LEFT JOIN t_user u ON u.id=a.appellant_id
            LEFT JOIN t_dispute d ON d.id=a.dispute_id
            WHERE {' AND '.join(where)}
            ORDER BY CASE a.status WHEN 'human_review' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                     a.id DESC LIMIT %s""", tuple(args + [limit]))
    return {"items": [{
        "id": r["id"], "target_type": r["target_type"], "target_id": r["target_id"],
        "shop_id": r["shop_id"], "shop_name": r.get("shop_name") or "",
        "dispute_id": r.get("dispute_id"), "dispute_no": r.get("dispute_no") or "",
        "appellant_id": r.get("appellant_id"),
        "appellant_name": r.get("appellant_name") or "",
        "appellant_role": r["appellant_role"], "reason": r.get("reason") or "",
        "evidence": gov.parse_evidence(r.get("evidence")),
        "status": r["status"], "ai_view": r.get("ai_view") or "",
        "ai_suggest": r.get("ai_suggest") or "",
        "ai_confidence": float(r.get("ai_confidence") or 0),
        "result": r.get("result") or "",
        "supplement_count": int(r.get("supplement_count") or 0),
        "supplement_history": gov.parse_evidence(r.get("supplement_history")),
        "overdue_hours": gov.appeal_overdue_hours(r),
        "created_at": str(r.get("created_at") or ""),
        "decided_at": str(r.get("decided_at") or ""),
    } for r in rows]}


class DecideIn(BaseModel):
    accept: bool
    result: str


@router.post("/appeals/{aid}/decide")
def decide(aid: int, body: DecideIn, user: dict = Depends(sec.require(*PLATFORM))):
    try:
        r = gov.appeal_decide(aid, user, body.accept, body.result)
        schema.audit(user["id"], "appeal_decide", str(aid), "",
                     ("受理 " if body.accept else "驳回 ") + body.result[:120])
        return r
    except ValueError as e:
        raise _bad(e)


class SupplementIn(BaseModel):
    evidence: list[Evidence] | None = None
    note: str = ""


@router.post("/appeals/{aid}/supplement")
def supplement(aid: int, body: SupplementIn, user: dict = Depends(sec.current_user)):
    """补充材料：处理中的申诉追加证据，重新初审。"""
    ev = [{"k": e.k[:30], "v": (e.v or "")[:300]} for e in (body.evidence or [])]
    try:
        r = gov.appeal_supplement(aid, user, ev, body.note)
        schema.audit(user["id"], "appeal_supplement", str(aid), "",
                     f"+{len(ev)} 条证据")
        return r
    except ValueError as e:
        raise _bad(e)


@router.post("/appeals/{aid}/reopen")
def reopen(aid: int, body: SupplementIn, user: dict = Depends(sec.current_user)):
    """二次复核：被驳回的申诉补充证据后申请复议一次。"""
    ev = [{"k": e.k[:30], "v": (e.v or "")[:300]} for e in (body.evidence or [])]
    try:
        r = gov.appeal_reopen(aid, user, ev, body.note)
        schema.audit(user["id"], "appeal_reopen", str(aid), "",
                     f"+{len(ev)} 条证据复议")
        return r
    except ValueError as e:
        raise _bad(e)


# ---------------------------------------------------------------- 店铺监管

@router.get("/shops")
def shops(user: dict = Depends(sec.current_user)):
    # 列表页是运营看「现在哪些店被停着」的地方，进页面前先把到期的收掉。
    # 定时任务兜底 + 这里兜一次：进程刚重启、定时器还没跑第一轮时也不会显示过期状态。
    try:
        gov.expire_due()
    except Exception:
        pass
    rows = common.fetch_all(
        """SELECT s.id, s.name, s.platform, s.category, c.credit_score, c.level,
                  c.status, c.points_year, c.dispute_count, c.dispute_lost,
                  c.penalty_count, c.appeal_count, c.suspend_until, c.updated_at
           FROM t_shop s LEFT JOIN t_shop_credit c ON c.shop_id=s.id ORDER BY s.id""")
    out = []
    for r in rows:
        st = r["status"] or "normal"
        out.append({
            "id": r["id"], "name": r["name"], "platform": r["platform"],
            "category": r.get("category") or "",
            "credit_score": r["credit_score"] if r["credit_score"] is not None else 100,
            "level": r["level"] or "良好", "status": st,
            "status_label": gov.STATUS_LABEL.get(st, st),
            "points_year": float(r["points_year"] or 0),
            "dispute_count": r["dispute_count"] or 0,
            "dispute_lost": r["dispute_lost"] or 0,
            "penalty_count": r["penalty_count"] or 0,
            "appeal_count": r["appeal_count"] or 0,
            "suspend_until": str(r.get("suspend_until") or ""),
            "self": r["id"] == user.get("shop_id"),
        })
    return {"items": out}


@router.get("/dashboard")
def dashboard(user: dict = Depends(sec.require(*PLATFORM))):
    m = gov.metrics()
    recent = common.fetch_all(
        "SELECT id, dispute_no, title, dtype, status, ai_confidence, created_at "
        "FROM t_dispute ORDER BY id DESC LIMIT 8")
    return {"metrics": m,
            "recent": [{"id": r["id"], "no": r["dispute_no"], "title": r["title"],
                        "type_label": gov.DTYPES.get(r["dtype"], {}).get("label", r["dtype"]),
                        "status": r["status"],
                        "confidence": float(r["ai_confidence"] or 0),
                        "created_at": str(r.get("created_at") or "")} for r in recent]}


# ---------------------------------------------------------------- 启动引导

def boot() -> None:
    """建信用档案 → 灌演示纠纷（只在库为空时）→ 补齐订单与来源会话。"""
    schema.init_credit()
    n = gov.seed_demo()
    if n:
        print(f"[gov] 演示纠纷初始化：{n} 单")
    b = gov.backfill_orders()
    if b:
        print(f"[gov] 补挂订单：{b} 单")
    bi = gov.backfill_order_items()
    if bi:
        print(f"[gov] 补商品明细与运单号：{bi} 单")
    s = gov.backfill_sessions()
    if s:
        print(f"[gov] 补来源客服会话：{s} 单")
    # 带单核查要在全量条款里检索，索引建一次就够，别让第一个运营等这 1~2 秒
    n_cl = gov.warm_clause_index()
    print(f"[gov] 带单核查条款索引：{n_cl} 条")
