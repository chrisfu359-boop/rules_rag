"""规则库浏览接口：给前端的"规则库"页面用。

和 /api/chat/ask 共用同一个检索器，但不做生成——用户想自己翻条款原文时用这个。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import common
import service.qa as qa
from api import security as sec

router = APIRouter(prefix="/api/rules", tags=["rules"])


@router.get("/docs")
def docs(user: dict = Depends(sec.current_user)):
    rows = common.fetch_all(
        """SELECT d.id, d.doc_name, d.doc_type, d.effective_from, d.source_url,
                  COUNT(c.id) AS n
           FROM t_rule_doc d LEFT JOIN t_clause c ON c.doc_id=d.id
           GROUP BY d.id ORDER BY d.id""")
    return {"items": [{"id": r["id"], "name": r["doc_name"], "type": r.get("doc_type") or "",
                       "effective_from": str(r.get("effective_from") or ""),
                       "source_url": r.get("source_url") or "",
                       "clauses": r["n"]} for r in rows]}


@router.get("/clauses")
def clauses(doc_id: int | None = None, risk: str = "", scene: str = "",
            page: int = 1, size: int = 20,
            user: dict = Depends(sec.current_user)):
    size = max(1, min(size, 100))
    page = max(1, page)
    where = ["1=1"]
    args: list = []
    if doc_id:
        where.append("c.doc_id=%s")
        args.append(doc_id)
    if risk:
        where.append("c.risk_level=%s")
        args.append(risk)
    if scene:
        where.append("c.scene=%s")
        args.append(scene)
    w = " AND ".join(where)

    total = common.fetch_all(f"SELECT COUNT(*) AS n FROM t_clause c WHERE {w}", tuple(args))[0]["n"]
    rows = common.fetch_all(
        f"""SELECT c.id, c.clause_no, c.clause_path, c.content, c.risk_level, c.scene,
                   c.platform, d.doc_name, d.source_url, d.effective_from
            FROM t_clause c JOIN t_rule_doc d ON c.doc_id=d.id
            WHERE {w} ORDER BY c.doc_id, c.order_index, c.id
            LIMIT %s OFFSET %s""", tuple(args) + (size, (page - 1) * size))
    return {"total": total, "page": page, "size": size, "items": [{
        "id": r["id"], "clause_no": r["clause_no"], "clause_path": r.get("clause_path"),
        "content": r["content"], "risk_level": r.get("risk_level"),
        "scene": r.get("scene"), "doc_name": r["doc_name"],
        "source_url": r.get("source_url"),
        "effective_from": str(r.get("effective_from") or ""),
    } for r in rows]}


class SearchIn(BaseModel):
    query: str
    top_k: int = 10


@router.post("/search")
def search(body: SearchIn, user: dict = Depends(sec.current_user)):
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(400, "请输入检索词")
    hits = qa.RT.index.search(q, top_k=max(1, min(body.top_k, 30)))
    return {"items": [{
        "doc_name": d["doc_name"], "clause_no": d["clause_no"],
        "clause_path": d.get("clause_path"), "content": d["content"],
        "risk_level": d.get("risk_level"), "scene": d.get("scene"),
        "effective_from": str(d.get("effective_from") or ""),
        "source_url": d.get("source_url"), "score": round(s, 4),
    } for d, s in hits]}


@router.get("/stats")
def rule_stats(user: dict = Depends(sec.current_user)):
    docs_n = common.fetch_all("SELECT COUNT(*) AS n FROM t_rule_doc")[0]["n"]
    cl = common.fetch_all("SELECT COUNT(*) AS n FROM t_clause")[0]["n"]
    risk = common.fetch_all("SELECT risk_level, COUNT(*) AS n FROM t_clause GROUP BY risk_level")
    scene = common.fetch_all("SELECT scene, COUNT(*) AS n FROM t_clause GROUP BY scene")
    plat = common.fetch_all("SELECT platform, COUNT(*) AS n FROM t_clause GROUP BY platform")
    return {"docs": docs_n, "clauses": cl,
            "risk": {r["risk_level"]: r["n"] for r in risk},
            "scene": {r["scene"]: r["n"] for r in scene},
            "platform": {str(r["platform"] or "法规"): r["n"] for r in plat}}
