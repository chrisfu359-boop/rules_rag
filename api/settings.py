"""个人设置：资料、偏好、店铺信息。"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import common
from api import schema, security as sec

router = APIRouter(prefix="/api/settings", tags=["settings"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SCENES = {"", "after_sale", "logistics", "ad_compliance", "product_info", "penalty"}


class ProfileIn(BaseModel):
    display_name: str
    email: str


class PrefIn(BaseModel):
    theme: str | None = "auto"
    default_scene: str | None = ""
    answer_style: str | None = "business"


class ShopIn(BaseModel):
    name: str
    platform: str | None = "淘宝"
    category: str | None = ""
    contact_phone: str | None = ""


@router.get("")
def get_all(user: dict = Depends(sec.current_user)):
    pref = common.fetch_all("SELECT * FROM t_user_pref WHERE user_id=%s", (user["id"],))
    shop = None
    if user.get("shop_id"):
        rows = common.fetch_all("SELECT * FROM t_shop WHERE id=%s", (user["shop_id"],))
        shop = rows[0] if rows else None
    return {
        "user": sec.public_user(user),
        "pref": pref[0] if pref else {"theme": "auto", "default_scene": "", "answer_style": "business"},
        "shop": shop,
        "scenes": [
            {"key": "", "label": "自动识别"},
            {"key": "after_sale", "label": "售后/退换"},
            {"key": "logistics", "label": "物流/发货"},
            {"key": "ad_compliance", "label": "广告合规"},
            {"key": "product_info", "label": "商品信息"},
            {"key": "penalty", "label": "违规/处罚"},
        ],
    }


@router.put("/profile")
def update_profile(body: ProfileIn, user: dict = Depends(sec.current_user)):
    name = (body.display_name or "").strip()
    email = (body.email or "").strip().lower()
    if not name:
        raise HTTPException(400, "昵称不能为空")
    if len(name) > 60:
        raise HTTPException(400, "昵称最多 60 字")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")
    dup = common.fetch_all("SELECT id FROM t_user WHERE email=%s AND id<>%s", (email, user["id"]))
    if dup:
        raise HTTPException(400, "该邮箱已被其他账号使用")
    common.execute("UPDATE t_user SET display_name=%s, email=%s WHERE id=%s",
                   (name, email, user["id"]))
    schema.audit(user["id"], "update_profile", user["username"])
    return {"ok": True}


@router.put("/pref")
def update_pref(body: PrefIn, user: dict = Depends(sec.current_user)):
    theme = body.theme if body.theme in ("auto", "light", "dark") else "auto"
    scene = body.default_scene if body.default_scene in SCENES else ""
    style = body.answer_style if body.answer_style in ("business", "strict", "brief") else "business"
    if common.fetch_all("SELECT user_id FROM t_user_pref WHERE user_id=%s", (user["id"],)):
        common.execute(
            """UPDATE t_user_pref SET theme=%s, default_scene=%s, answer_style=%s,
                   updated_at=%s WHERE user_id=%s""",
            (theme, scene, style, schema.now(), user["id"]))
    else:
        common.execute(
            """INSERT INTO t_user_pref (user_id, theme, default_scene, answer_style, updated_at)
               VALUES (%s,%s,%s,%s,%s)""", (user["id"], theme, scene, style, schema.now()))
    return {"ok": True, "pref": {"theme": theme, "default_scene": scene, "answer_style": style}}


@router.put("/shop")
def update_shop(body: ShopIn, user: dict = Depends(sec.current_user)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "店铺名称不能为空")
    if user["role"] == "merchant" and user.get("shop_id"):
        common.execute(
            "UPDATE t_shop SET name=%s, platform=%s, category=%s, contact_phone=%s WHERE id=%s",
            (name[:120], (body.platform or "淘宝")[:30], (body.category or "")[:60],
             (body.contact_phone or "")[:30], user["shop_id"]))
        sid = user["shop_id"]
    else:
        sid = common.insert(
            "INSERT INTO t_shop (name, platform, category, contact_phone, created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (name[:120], (body.platform or "淘宝")[:30], (body.category or "")[:60],
             (body.contact_phone or "")[:30], schema.now()))
        common.execute("UPDATE t_user SET shop_id=%s WHERE id=%s", (sid, user["id"]))
    schema.audit(user["id"], "update_shop", name)
    return {"ok": True, "shop_id": sid}
