"""买家订单中心：我的订单 / 历史订单 / 申请退货退款。

归属靠 `t_order.buyer_id`；商家侧仍走 `/api/gov/orders`（按 shop 收口），
运营/管理员可看全平台。本 router 只服务「以登录人为主体」的订单查询与售后申请。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import common
from api import security as sec

router = APIRouter(prefix="/api/orders", tags=["orders"])

# 商品状态口径，与 t_order.status 一致
LIVE = ("待付款", "待发货", "已发货", "售后中")
DONE = ("已完成", "已关闭")
AFTERSALE_KINDS = ("仅退款", "退货退款", "换货")


class RefundIn(BaseModel):
    kind: str = "退货退款"
    reason: str = ""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _own(order_no: str) -> dict:
    rows = common.fetch_all("SELECT * FROM t_order WHERE order_no=%s", (order_no,))
    if not rows:
        raise HTTPException(404, "订单不存在")
    return rows[0]


@router.get("/mine")
def mine(status: str = "", user: dict = Depends(sec.current_user)):
    """我的订单：默认全部；status 可筛（待发货/已发货/已完成/售后中…）。

    买家只能看到挂在自己 buyer_id 下的单；商家/运营请用各自的工作台接口。
    """
    if user["role"] != "buyer":
        raise HTTPException(403, "仅买家账号可查看「我的订单」")
    where, args = ["buyer_id=%s"], [user["id"]]
    if status:
        where.append("status=%s")
        args.append(status)
    rows = common.fetch_all(
        "SELECT order_no, shop_name, item_title, sku, qty, unit_price, amount, status, "
        "paid_at, shipped_at, received_at, carrier, tracking_no, aftersale_type, "
        "aftersale_applied_at FROM t_order WHERE " + " AND ".join(where) +
        " ORDER BY paid_at DESC, order_no DESC", tuple(args))
    counts = {r["status"]: r["n"] for r in common.fetch_all(
        "SELECT status, COUNT(*) AS n FROM t_order WHERE buyer_id=%s GROUP BY status",
        (user["id"],))}
    return {
        "items": rows,
        "counts": counts,
        "total": sum(counts.values()),
        "live": sum(counts.get(s, 0) for s in LIVE),
        "done": sum(counts.get(s, 0) for s in DONE),
    }


@router.get("/{order_no}")
def detail(order_no: str, user: dict = Depends(sec.current_user)):
    o = _own(order_no)
    if user["role"] == "buyer" and o.get("buyer_id") != user["id"]:
        raise HTTPException(404, "订单不存在")
    items = common.fetch_all(
        "SELECT line_no, title, sku, qty, unit_price FROM t_order_item "
        "WHERE order_no=%s ORDER BY line_no", (order_no,))
    return {"order": o, "items": items}


@router.post("/{order_no}/refund")
def apply_refund(order_no: str, body: RefundIn, user: dict = Depends(sec.current_user)):
    """买家申请退货退款/换货：订单置「售后中」，记录类型、原因与申请时间。"""
    o = _own(order_no)
    if user["role"] == "buyer" and o.get("buyer_id") != user["id"]:
        raise HTTPException(404, "订单不存在")
    if o["status"] in ("待付款", "已关闭"):
        raise HTTPException(400, "该订单当前不可申请售后")
    if o["status"] == "售后中":
        raise HTTPException(400, "该订单已在售后处理中")
    kind = (body.kind or "退货退款").strip()
    if kind not in AFTERSALE_KINDS:
        kind = "退货退款"
    ts = _now()
    common.execute(
        "UPDATE t_order SET status='售后中', aftersale_type=%s, aftersale_applied_at=%s, "
        "remarks=%s WHERE order_no=%s",
        (kind, ts, (body.reason or "")[:200], order_no))
    return {"ok": True, "order_no": order_no, "aftersale_type": kind, "applied_at": ts}
