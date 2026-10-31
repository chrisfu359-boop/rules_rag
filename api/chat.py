"""会话与问答接口。

数据隔离的唯一原则：**每一条查询都必须带 user_id 条件**。
不是"先查出来再判断 owner"，那样只要有一处忘了判断就是越权。
"""
from __future__ import annotations

import hashlib
import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import common
import service.cache as cache
import service.qa as qa
from api import schema, security as sec

router = APIRouter(prefix="/api/chat", tags=["chat"])

RATE_LIMIT = 30      # 每用户每分钟提问上限


def _own_session(sid: str, user: dict) -> dict:
    rows = common.fetch_all(
        "SELECT * FROM t_chat_session WHERE id=%s AND user_id=%s", (sid, user["id"]))
    if not rows:
        raise HTTPException(404, "会话不存在")
    return rows[0]


def _msg_out(m: dict) -> dict:
    cites = []
    if m.get("citations"):
        try:
            cites = json.loads(m["citations"])
        except Exception:
            cites = []
    return {
        "id": m["id"], "role": m["role"], "content": m.get("content") or "",
        "risk_level": m.get("risk_level") or "", "citations": cites,
        "generator": m.get("generator") or "", "latency_ms": m.get("latency_ms") or 0,
        "created_at": str(m.get("created_at") or ""),
        "feedback": m.get("feedback"),
    }


class NewSessionIn(BaseModel):
    title: str | None = None
    scene: str | None = None


class RenameIn(BaseModel):
    title: str


class AskIn(BaseModel):
    query: str
    session_id: str | None = None


class FeedbackIn(BaseModel):
    score: int
    comment: str | None = ""


@router.get("/sessions")
def list_sessions(limit: int = 30, offset: int = 0, q: str = "",
                  user: dict = Depends(sec.current_user)):
    limit = max(1, min(limit, 100))
    if q.strip():
        rows = common.fetch_all(
            """SELECT * FROM t_chat_session WHERE user_id=%s AND title LIKE %s
               ORDER BY updated_at DESC LIMIT %s OFFSET %s""",
            (user["id"], f"%{q.strip()}%", limit, offset))
    else:
        rows = common.fetch_all(
            """SELECT * FROM t_chat_session WHERE user_id=%s
               ORDER BY updated_at DESC LIMIT %s OFFSET %s""",
            (user["id"], limit, offset))
    total = common.fetch_all(
        "SELECT COUNT(*) AS n FROM t_chat_session WHERE user_id=%s", (user["id"],))[0]["n"]
    return {"total": total, "items": [{
        "id": r["id"], "title": r["title"], "scene": r.get("scene") or "",
        "message_count": r.get("message_count") or 0,
        "created_at": str(r.get("created_at") or ""),
        "updated_at": str(r.get("updated_at") or ""),
    } for r in rows]}


@router.post("/sessions")
def create_session(body: NewSessionIn, user: dict = Depends(sec.current_user)):
    sid = schema.new_sid()
    common.execute(
        """INSERT INTO t_chat_session (id, user_id, title, scene, message_count,
               created_at, updated_at) VALUES (%s,%s,%s,%s,0,%s,%s)""",
        (sid, user["id"], (body.title or "新会话")[:180], body.scene or "",
         schema.now(), schema.now()))
    return {"id": sid, "title": body.title or "新会话"}


@router.get("/sessions/{sid}")
def get_session(sid: str, user: dict = Depends(sec.current_user)):
    s = _own_session(sid, user)
    rows = common.fetch_all(
        """SELECT m.*, f.score AS fb_score FROM t_chat_message m
           LEFT JOIN t_feedback f ON f.message_id=m.id AND f.user_id=%s
           WHERE m.session_id=%s ORDER BY m.id ASC""", (user["id"], sid))
    return {"session": {"id": s["id"], "title": s["title"], "scene": s.get("scene") or "",
                        "message_count": s.get("message_count") or 0,
                        "created_at": str(s.get("created_at") or "")},
            "messages": [_msg_out({**r, "feedback": r.get("fb_score")}) for r in rows]}


@router.patch("/sessions/{sid}")
def rename_session(sid: str, body: RenameIn, user: dict = Depends(sec.current_user)):
    _own_session(sid, user)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    common.execute("UPDATE t_chat_session SET title=%s, updated_at=%s WHERE id=%s AND user_id=%s",
                   (title[:180], schema.now(), sid, user["id"]))
    return {"ok": True, "title": title[:180]}


@router.delete("/sessions/{sid}")
def delete_session(sid: str, user: dict = Depends(sec.current_user)):
    _own_session(sid, user)
    common.execute("DELETE FROM t_chat_message WHERE session_id=%s", (sid,))
    common.execute("DELETE FROM t_chat_session WHERE id=%s AND user_id=%s", (sid, user["id"]))
    return {"ok": True}


@router.post("/ask")
def ask(body: AskIn, user: dict = Depends(sec.current_user)):
    t0 = time.time()
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    if len(q) > 500:
        raise HTTPException(400, "问题过长（最多 500 字），请精简后重试")

    ok, _ = cache.rate_ok(f"ask:{user['id']}", RATE_LIMIT)
    if not ok:
        raise HTTPException(429, f"提问过于频繁，每分钟最多 {RATE_LIMIT} 次")

    # 会话：没带 id 就新建一个，标题用第一句话
    if body.session_id:
        _own_session(body.session_id, user)
        sid = body.session_id
    else:
        sid = schema.new_sid()
        common.execute(
            """INSERT INTO t_chat_session (id, user_id, title, scene, message_count,
                   created_at, updated_at) VALUES (%s,%s,%s,'',0,%s,%s)""",
            (sid, user["id"], q[:40], schema.now(), schema.now()))

    first_in_session = common.fetch_all(
        "SELECT message_count FROM t_chat_session WHERE id=%s", (sid,))[0]["message_count"] == 0

    # 多轮记忆：捞最近 N 轮对话（当前这条还没入库，捞到的全是历史）。
    # 10 轮 = 20 条消息，倒序取再反转成时间正序，喂给生成层做指代消解。
    hist_rows = common.fetch_all(
        "SELECT role, content FROM t_chat_message WHERE session_id=%s AND user_id=%s "
        "ORDER BY id DESC LIMIT %s",
        (sid, user["id"], qa.HISTORY_ROUNDS * 2))
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(hist_rows)]

    # 存用户消息。role 只有 user/assistant 两种，不给前端自由传
    common.insert(
        """INSERT INTO t_chat_message (session_id, user_id, role, content, created_at)
           VALUES (%s,%s,'user',%s,%s)""", (sid, user["id"], q, schema.now()))

    # FAQ 缓存：key 带规则库版本 + 生成模型，规则或模型变了就自动隔离
    ver = cache.rule_version()
    model_tag = (qa.RT.llm.model or "template") if qa.RT.llm else "template"
    ck = f"rr:{ver}:chat:{hashlib.md5(model_tag.encode()).hexdigest()[:8]}:" \
         f"{hashlib.md5(q.encode()).hexdigest()}"
    res = cache.get(ck)
    cache_hit = res is not None
    if res is None:
        res = qa.answer(q, history=history)
        cache.set(ck, res, ttl=600)
    else:
        # 缓存里存的是"上一次算这个答案花的时间"，直接返回等于告诉用户"这次也花了 4.8 秒"。
        # 命中缓存必须报本次真实耗时，否则缓存这个优化在界面上完全体现不出来。
        res = {**res, "latency_ms": int((time.time() - t0) * 1000)}

    cites = [{"doc_name": c["doc_name"], "clause_no": c["clause_no"],
              "clause_path": c.get("clause_path"), "content": c["content"],
              "risk_level": c.get("risk_level"), "effective_from": c.get("effective_from"),
              "source_url": c.get("source_url"), "score": c["score"]} for c in res["clauses"]]

    mid = common.insert(
        """INSERT INTO t_chat_message (session_id, user_id, role, content, risk_level,
               citations, generator, latency_ms, created_at)
           VALUES (%s,%s,'assistant',%s,%s,%s,%s,%s,%s)""",
        (sid, user["id"], res.get("conclusion") or "", res.get("risk_level") or "",
         json.dumps(cites, ensure_ascii=False), res.get("generator") or "",
         res.get("latency_ms") or 0, schema.now()))

    common.execute(
        """UPDATE t_chat_session SET message_count=message_count+2, updated_at=%s,
               title=CASE WHEN message_count=0 THEN %s ELSE title END
           WHERE id=%s""", (schema.now(), q[:40], sid))

    qa.log_qa(sid, user["id"], res, cache_hit=cache_hit)

    # 返回体里同时给 clauses（完整字段）和 citations（前端渲染用）。
    # 少给一个，前端就得自己拼，两处结构一旦不一致就是"对话里看不见条款"这种哑bug。
    res["citations"] = cites
    res.update({"session_id": sid, "message_id": mid,
                "session_title": q[:40] if first_in_session else None,
                "cache_hit": cache_hit})
    return res


@router.post("/messages/{mid}/feedback")
def feedback(mid: int, body: FeedbackIn, user: dict = Depends(sec.current_user)):
    if body.score not in (1, -1):
        raise HTTPException(400, "score 只能是 1 或 -1")
    rows = common.fetch_all(
        "SELECT id FROM t_chat_message WHERE id=%s AND user_id=%s", (mid, user["id"]))
    if not rows:
        raise HTTPException(404, "消息不存在")
    # 一个人对一条消息只有一票，改主意就是覆盖（uk_message_user 保证）
    try:
        common.execute("DELETE FROM t_feedback WHERE message_id=%s AND user_id=%s", (mid, user["id"]))
        common.execute(
            "INSERT INTO t_feedback (message_id, user_id, score, comment, created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (mid, user["id"], body.score, (body.comment or "")[:480], schema.now()))
    except Exception:
        raise HTTPException(500, "反馈保存失败")
    return {"ok": True}


@router.get("/overview")
def overview(user: dict = Depends(sec.current_user)):
    """个人看板：会话数、提问数、平均响应、风险分布、生成方式分布。"""
    uid = user["id"]
    n_sess = common.fetch_all("SELECT COUNT(*) AS n FROM t_chat_session WHERE user_id=%s", (uid,))[0]["n"]
    asked = common.fetch_all(
        "SELECT COUNT(*) AS n FROM t_chat_message WHERE user_id=%s AND role='user'", (uid,))[0]["n"]
    ans = common.fetch_all(
        """SELECT COUNT(*) AS n, ROUND(AVG(latency_ms)) AS avg_ms,
                  SUM(CASE WHEN risk_level='L3' THEN 1 ELSE 0 END) AS l3
           FROM t_chat_message WHERE user_id=%s AND role='assistant'""", (uid,))[0]
    gen = common.fetch_all(
        """SELECT generator, COUNT(*) AS n FROM t_chat_message
           WHERE user_id=%s AND role='assistant' GROUP BY generator""", (uid,))
    fb = common.fetch_all(
        """SELECT SUM(CASE WHEN score=1 THEN 1 ELSE 0 END) AS up,
                  SUM(CASE WHEN score=-1 THEN 1 ELSE 0 END) AS down
           FROM t_feedback WHERE user_id=%s""", (uid,))[0]
    return {
        "sessions": n_sess, "questions": asked,
        "answers": ans["n"] or 0,
        "avg_latency_ms": ans["avg_ms"] or 0,
        "l3_count": ans["l3"] or 0,
        "generator": {g["generator"] or "template": g["n"] for g in gen},
        "feedback": {"up": fb["up"] or 0, "down": fb["down"] or 0},
    }
