"""管理后台：用户管理、模型配置、审计日志、全站指标。

权限全部走 require("admin")，不做"前端隐藏按钮"这种伪权限——
前端隐藏只是体验，后端不校验就是漏洞。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import common
import service.cache as cache
import service.qa as qa
from api import schema, security as sec

router = APIRouter(prefix="/api/admin", tags=["admin"])

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
_ADMIN = Depends(sec.require("admin"))


def _mask(v: str) -> str:
    if not v:
        return ""
    return f"{'*' * max(0, len(v) - 4)}{v[-4:]}" if len(v) > 8 else "*" * len(v)


@router.get("/users")
def users(q: str = "", role: str = "", status: str = "", limit: int = 50,
          _: dict = _ADMIN):
    limit = max(1, min(limit, 200))
    sql = ("SELECT u.*, s.name AS shop_name FROM t_user u "
           "LEFT JOIN t_shop s ON s.id=u.shop_id WHERE 1=1")
    args: list = []
    if q.strip():
        sql += " AND (u.username LIKE %s OR u.email LIKE %s OR u.display_name LIKE %s)"
        args += [f"%{q.strip()}%"] * 3
    if role:
        sql += " AND u.role=%s"
        args.append(role)
    if status:
        sql += " AND u.status=%s"
        args.append(status)
    sql += " ORDER BY u.id DESC LIMIT %s"
    args.append(limit)
    rows = common.fetch_all(sql, tuple(args))
    return {"total": len(rows), "items": [sec.public_user(r) for r in rows]}


class UserPatch(BaseModel):
    status: str | None = None
    role: str | None = None
    display_name: str | None = None


@router.patch("/users/{uid}")
def patch_user(uid: int, body: UserPatch, admin: dict = _ADMIN):
    rows = common.fetch_all("SELECT * FROM t_user WHERE id=%s", (uid,))
    if not rows:
        raise HTTPException(404, "用户不存在")
    target = rows[0]

    if uid == admin["id"]:
        # 自锁防护：把自己停用或降级，就没有管理员能再改回来
        raise HTTPException(400, "不能修改自己的角色或状态")

    if body.status is not None:
        if body.status not in ("active", "disabled"):
            raise HTTPException(400, "状态只能是 active / disabled")
        common.execute("UPDATE t_user SET status=%s WHERE id=%s", (body.status, uid))
        schema.audit(admin["id"], "admin_user_status", target["username"],
                     detail=f"{target['status']} -> {body.status}")

    if body.role is not None:
        if body.role not in ("merchant", "ops", "admin"):
            raise HTTPException(400, "角色不合法")
        if target["role"] == "admin" and body.role != "admin":
            remains = common.fetch_all(
                "SELECT COUNT(*) AS n FROM t_user WHERE role='admin' AND status='active'")[0]["n"]
            if remains <= 1:
                raise HTTPException(400, "系统至少要保留一个启用的管理员")
        common.execute("UPDATE t_user SET role=%s WHERE id=%s", (body.role, uid))
        schema.audit(admin["id"], "admin_user_role", target["username"],
                     detail=f"{target['role']} -> {body.role}")

    if body.display_name is not None:
        n = body.display_name.strip()
        if not n:
            raise HTTPException(400, "昵称不能为空")
        common.execute("UPDATE t_user SET display_name=%s WHERE id=%s", (n[:60], uid))

    return {"ok": True}


@router.get("/audit")
def audit_log(limit: int = 100, action: str = "", _: dict = _ADMIN):
    limit = max(1, min(limit, 500))
    sql = ("SELECT a.*, u.username FROM t_audit_log a "
           "LEFT JOIN t_user u ON u.id=a.user_id WHERE 1=1")
    args: list = []
    if action.strip():
        sql += " AND a.action=%s"
        args.append(action.strip())
    sql += " ORDER BY a.id DESC LIMIT %s"
    args.append(limit)
    return {"items": common.fetch_all(sql, tuple(args))}


@router.get("/metrics")
def metrics(_: dict = _ADMIN):
    def one(sql, args=()):
        r = common.fetch_all(sql, args)
        return r[0] if r else {}

    return {
        "users": one("SELECT COUNT(*) AS total, "
                     "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active FROM t_user"),
        "users_by_role": common.fetch_all(
            "SELECT role, COUNT(*) AS n FROM t_user GROUP BY role"),
        "sessions": one("SELECT COUNT(*) AS n FROM t_chat_session"),
        "messages": one("SELECT COUNT(*) AS n FROM t_chat_message"),
        "qa": one("SELECT COUNT(*) AS n, ROUND(AVG(latency_ms)) AS avg_ms FROM t_qa_log"),
        "risk": common.fetch_all(
            "SELECT risk_level, COUNT(*) AS n FROM t_clause GROUP BY risk_level"),
        "feedback": one("SELECT SUM(CASE WHEN score=1 THEN 1 ELSE 0 END) AS up, "
                        "SUM(CASE WHEN score=-1 THEN 1 ELSE 0 END) AS down FROM t_feedback"),
        "runtime": qa.RT.stats(),
        "cache": cache.stats(),
        "db": {"backend": common.backend()},
    }


# ---------------- 模型配置 ----------------

_WRITABLE = ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
             "LLM", "LLM_TIMEOUT", "LLM_MAX_TOKENS", "LLM_TEMPERATURE")
_SECRET_KEYS = {"LLM_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY",
                "MOONSHOT_API_KEY", "ZHIPU_API_KEY", "OPENAI_API_KEY"}


def _read_env() -> dict:
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


@router.get("/config")
def get_config(_: dict = _ADMIN):
    env = _read_env()
    llm = qa.RT.llm
    return {
        "env": {k: (_mask(env.get(k, "")) if k in _SECRET_KEYS else env.get(k, ""))
                for k in _WRITABLE},
        "effective": {
            "provider": getattr(llm, "provider", ""),
            "model": getattr(llm, "model", ""),
            "base_url": getattr(llm, "base_url", ""),
            "available": llm.available() if llm else False,
        },
        "presets": ["deepseek", "dashscope", "moonshot", "zhipu", "openai", "ollama"],
        "editable": list(_WRITABLE),
    }


class ConfigIn(BaseModel):
    LLM: str | None = None
    LLM_PROVIDER: str | None = None
    LLM_BASE_URL: str | None = None
    LLM_API_KEY: str | None = None
    LLM_MODEL: str | None = None
    LLM_TIMEOUT: str | None = None
    LLM_MAX_TOKENS: str | None = None
    LLM_TEMPERATURE: str | None = None


@router.put("/config")
def put_config(body: ConfigIn, admin: dict = _ADMIN):
    """写 .env 并热重载模型客户端。

    为什么不是只放数据库里：.env 是唯一权威，进程重启后仍然生效。
    如果只写数据库，重启就回到旧配置，会出现"后台看着是 A 模型、实际跑的是 B"。
    """
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "没有要修改的项")

    # 传了掩码值（含 *）说明用户没改，跳过，避免把真 key 覆盖成星号
    changes = {k: v for k, v in changes.items() if "*" not in str(v)}

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    for k, v in changes.items():
        pat = re.compile(rf"^\s*{k}\s*=")
        for i, line in enumerate(lines):
            if pat.match(line):
                lines[i] = f"{k}={v}"
                break
        else:
            lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for k, v in changes.items():
        os.environ[k] = str(v)

    # 重建客户端：清掉缓存实例和失败标记，让新配置立刻生效
    from generation import llm as llm_mod
    llm_mod._client = None
    qa.RT.llm = llm_mod.get_llm()
    if qa.RT.llm.available():
        try:
            qa.RT.llm.client          # 预热，避免下一个用户承担 SDK 初始化开销
        except Exception:
            pass

    schema.audit(admin["id"], "admin_config", ",".join(changes.keys()),
                 detail=";".join(f"{k}={'***' if k in _SECRET_KEYS else v}"
                                 for k, v in changes.items())[:400])
    return {"ok": True, "changed": list(changes.keys()),
            "available": qa.RT.llm.available(), "model": qa.RT.llm.model}


@router.post("/config/test")
def test_config(_: dict = _ADMIN):
    from generation import llm as llm_mod
    llm_mod._client = None
    qa.RT.llm = llm_mod.get_llm()
    c = qa.RT.llm
    if not c.available():
        return {"ok": False, "reason": "未配置或已关闭"}
    text, meta = c.chat([{"role": "user", "content": "只回复两个字：收到"}], max_tokens=16)
    return {"ok": bool(text), "model": c.model, "ms": meta.get("ms"),
            "reply": (text or "")[:40], "error": meta.get("error", "")}
