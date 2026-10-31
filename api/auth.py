"""认证接口：注册 / 登录 / 登出 / 当前用户 / 改密码。"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

import common
from api import schema, security as sec

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _check_password_strength(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(400, "密码至少 8 位")
    if len(pw) > 64:
        raise HTTPException(400, "密码最多 64 位")
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        raise HTTPException(400, "密码需同时包含字母和数字")


class RegisterIn(BaseModel):
    username: str
    email: str
    password: str
    display_name: str | None = None
    shop_name: str | None = None
    platform: str | None = "淘宝"

    @field_validator("username")
    @classmethod
    def _u(cls, v: str) -> str:
        v = v.strip()
        if not USERNAME_RE.match(v):
            raise ValueError("用户名需以字母开头，3-32 位字母/数字/下划线")
        return v

    @field_validator("email")
    @classmethod
    def _e(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("邮箱格式不正确")
        return v


class LoginIn(BaseModel):
    username: str
    password: str


class ChangePwIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/register")
def register(body: RegisterIn, request: Request, response: Response):
    _check_password_strength(body.password)

    if common.fetch_all("SELECT id FROM t_user WHERE username=%s", (body.username,)):
        raise HTTPException(400, "该用户名已被占用")
    if common.fetch_all("SELECT id FROM t_user WHERE email=%s", (body.email,)):
        raise HTTPException(400, "该邮箱已被注册")

    ts = schema.now()
    shop_id = None
    if body.shop_name and body.shop_name.strip():
        shop_id = common.insert(
            "INSERT INTO t_shop (name, platform, created_at) VALUES (%s,%s,%s)",
            (body.shop_name.strip()[:120], (body.platform or "淘宝")[:30], ts))

    uid = common.insert(
        """INSERT INTO t_user (username, email, password_hash, display_name, role,
                               shop_id, avatar_color, created_at)
           VALUES (%s,%s,%s,%s,'merchant',%s,%s,%s)""",
        (body.username, body.email, schema.hash_password(body.password),
         (body.display_name or body.username).strip()[:60], shop_id,
         ["#4f7cff", "#0ea5a5", "#f59e0b", "#a855f7", "#ef4444"][common.fetch_all(
             "SELECT COUNT(*) AS n FROM t_user")[0]["n"] % 5], ts))
    common.insert("INSERT INTO t_user_pref (user_id, updated_at) VALUES (%s,%s)", (uid, ts))

    user = common.fetch_all(
        """SELECT u.*, s.name AS shop_name FROM t_user u
           LEFT JOIN t_shop s ON s.id=u.shop_id WHERE u.id=%s""", (uid,))[0]
    sec.set_cookie(response, sec.make_token(user))
    schema.audit(uid, "register", body.username, _ip(request))
    return {"ok": True, "user": sec.public_user(user)}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    ip = _ip(request)
    key = sec.fail_key(body.username, ip)
    wait = sec.too_many(key)
    if wait:
        raise HTTPException(429, f"连续登录失败过多，请 {wait} 秒后再试")

    rows = common.fetch_all(
        """SELECT u.*, s.name AS shop_name FROM t_user u
           LEFT JOIN t_shop s ON s.id=u.shop_id WHERE u.username=%s""",
        (body.username.strip(),))
    if not rows or not schema.check_password(body.password, rows[0]["password_hash"]):
        sec.note_fail(key)
        schema.audit(rows[0]["id"] if rows else None, "login_fail", body.username, ip)
        # 不区分"用户不存在"和"密码错误"，避免被用来枚举账号
        raise HTTPException(401, "用户名或密码错误")

    user = rows[0]
    if user["status"] != "active":
        raise HTTPException(403, "账号已被停用，请联系管理员")

    sec.clear_fail(key)
    common.execute("UPDATE t_user SET last_login_at=CURRENT_TIMESTAMP WHERE id=%s", (user["id"],))
    sec.set_cookie(response, sec.make_token(user))
    schema.audit(user["id"], "login", user["username"], ip)
    return {"ok": True, "user": sec.public_user(user)}


@router.post("/logout")
def logout(request: Request, response: Response):
    u = sec.optional_user(request)
    sec.clear_cookie(response)
    if u:
        schema.audit(u["id"], "logout", u["username"], _ip(request))
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(sec.current_user)):
    pref = common.fetch_all("SELECT * FROM t_user_pref WHERE user_id=%s", (user["id"],))
    return {"user": sec.public_user(user),
            "pref": pref[0] if pref else {"theme": "auto", "default_scene": "", "answer_style": "business"}}


@router.post("/password")
def change_password(body: ChangePwIn, request: Request,
                    user: dict = Depends(sec.current_user)):
    _check_password_strength(body.new_password)
    if not schema.check_password(body.old_password, user["password_hash"]):
        raise HTTPException(400, "原密码不正确")
    if body.old_password == body.new_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    common.execute("UPDATE t_user SET password_hash=%s WHERE id=%s",
                   (schema.hash_password(body.new_password), user["id"]))
    schema.audit(user["id"], "change_password", user["username"], _ip(request))
    return {"ok": True}
