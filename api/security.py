"""认证与授权。

三个刻意的设计选择：

1. **JWT 放 httpOnly cookie，不放 localStorage**
   localStorage 的 token 任何 XSS 都能读走；httpOnly cookie JS 读不到。
   代价是要防 CSRF，所以 SameSite=Lax + 所有写操作走非 GET。
   前后端同源部署，不需要处理跨域携带凭证的麻烦。

2. **角色每次从数据库读，不信 token 里的 role**
   token 有效期 7 天。如果这 7 天里把某个用户从 admin 降成 merchant，
   而权限判断读 token，他还能当 7 天管理员。权限必须实时。

3. **密钥落盘而不是硬编码**
   硬编码的 SECRET 一旦进 git 就等于公开，谁都能伪造 token。
   首次启动生成随机密钥写进 .jwt_secret（600 权限），重启不失效，也不进版本库。
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException, Request

import common

ROOT = Path(__file__).resolve().parent.parent
COOKIE_NAME = "rr_token"
ALG = "HS256"
TTL_HOURS = int(os.getenv("JWT_TTL_HOURS", "168"))     # 7 天
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"  # 上线 https 时置 1


def _load_secret() -> str:
    env = os.getenv("JWT_SECRET", "").strip()
    if env:
        return env
    p = ROOT / ".jwt_secret"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    s = secrets.token_urlsafe(48)
    p.write_text(s, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return s


SECRET = _load_secret()

ROLE_LABEL = {"buyer": "买家", "merchant": "商家", "ops": "平台运营", "admin": "管理员"}

# 可以进坐席工作台的角色。买家不行——买家能看会话列表，等于能看别家店铺的对话。
AGENT_ROLES = ("merchant", "ops", "admin")


def make_token(user: dict) -> str:
    now = int(time.time())
    return jwt.encode({"sub": str(user["id"]), "u": user["username"],
                       "iat": now, "exp": now + TTL_HOURS * 3600},
                      SECRET, algorithm=ALG)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET, algorithms=[ALG])
    except Exception:
        return None


def set_cookie(resp, token: str) -> None:
    resp.set_cookie(COOKIE_NAME, token, max_age=TTL_HOURS * 3600,
                    httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/")


def clear_cookie(resp) -> None:
    resp.delete_cookie(COOKIE_NAME, path="/")


def public_user(u: dict) -> dict:
    """对外暴露的用户字段白名单。password_hash 永远不出这一层——
    靠"记得别返回"是不可靠的，要用白名单。"""
    return {
        "id": u["id"], "username": u["username"], "email": u["email"],
        "display_name": u["display_name"], "role": u["role"],
        "role_label": ROLE_LABEL.get(u["role"], u["role"]),
        "shop_id": u.get("shop_id"), "shop_name": u.get("shop_name"),
        "avatar_color": u.get("avatar_color") or "#4f7cff",
        "status": u["status"],
        "created_at": str(u.get("created_at") or ""),
        "last_login_at": str(u.get("last_login_at") or ""),
    }


def _fetch_user(uid: int) -> dict | None:
    rows = common.fetch_all(
        """SELECT u.*, s.name AS shop_name FROM t_user u
           LEFT JOIN t_shop s ON s.id = u.shop_id WHERE u.id=%s""", (uid,))
    return rows[0] if rows else None


def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME) or ""
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录")

    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")

    u = _fetch_user(int(payload["sub"]))
    if not u:
        raise HTTPException(status_code=401, detail="账号不存在")
    if u["status"] != "active":
        raise HTTPException(status_code=403, detail="账号已被停用，请联系管理员")
    return u


def optional_user(request: Request) -> dict | None:
    try:
        return current_user(request)
    except HTTPException:
        return None


def require(*roles: str):
    """角色守卫。用法：Depends(require("admin"))"""
    def dep(user: dict = Depends(current_user)) -> dict:
        if roles and user["role"] not in roles:
            raise HTTPException(status_code=403, detail="没有权限执行该操作")
        return user
    return dep


# ---------------- 登录失败限流 ----------------

_FAIL: dict[str, list[float]] = {}
MAX_FAIL = 5
WINDOW = 300          # 5 分钟内连续 5 次失败即锁定


def fail_key(username: str, ip: str) -> str:
    return f"{username.lower()}|{ip}"


def too_many(key: str) -> int:
    """返回还需等待的秒数，0 表示未锁定。"""
    arr = [t for t in _FAIL.get(key, []) if time.time() - t < WINDOW]
    _FAIL[key] = arr
    if len(arr) >= MAX_FAIL:
        return int(WINDOW - (time.time() - arr[0])) + 1
    return 0


def note_fail(key: str) -> None:
    _FAIL.setdefault(key, []).append(time.time())


def clear_fail(key: str) -> None:
    _FAIL.pop(key, None)
