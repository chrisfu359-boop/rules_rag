"""rules_rag 服务：FastAPI + 用户体系 + 混合检索 + 精排 + 大模型生成。

启动：
    python run.py                      # 推荐，自动处理端口/Docker/浏览器
    DB_BACKEND=mysql python app.py     # 手动

页面：
    /login     登录注册
    /app       工作台（需登录）
    /rules     规则库（需登录）
    /settings  设置（需登录）
    /admin     管理后台（需 admin 角色）
    /legacy    早期单页 Demo（保留用于对比）

环境变量：
    DB_BACKEND / VECTOR_BACKEND / RERANK
    JWT_SECRET（不设则自动生成到 .jwt_secret）/ JWT_TTL_HOURS / COOKIE_SECURE
    LLM_PROVIDER / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
    API_KEY / RATE_LIMIT / MYSQL_* / REDIS_* / MILVUS_HOST
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

os.environ.setdefault("DB_BACKEND", "sqlite")

import common
import service.cache as cache
import service.cs as cs
import service.gov as gov_svc
import service.qa as qa
from api import admin as api_admin
from api import auth as api_auth
from api import chat as api_chat
from api import cs as api_cs
from api import gov as api_gov
from api import orders as api_orders
from api import rules as api_rules
from api import schema as db_schema
from api import security as sec
from api import settings as api_settings
from generation.llm import get_llm
from nlp import router as nlp_router
from retrieval.bm25 import BM25
from retrieval.hybrid import HybridRetriever
from retrieval.rerank import Reranker
from retrieval.vector_store import get_vector_index

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
RULE_VERSION = "v5"
API_KEY = os.getenv("API_KEY", "").strip()
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "30"))
RERANK_ON = os.getenv("RERANK", "on").lower() not in ("0", "off", "false")

# 启动/关闭钩子改用 lifespan。
# @app.on_event("startup") 在 FastAPI 0.115 已废弃，新版本会持续告警，
# 而且它和 yield 型生命周期没有共同顺序保证 —— 上线前迁移掉。
@asynccontextmanager
async def lifespan(_app: "FastAPI"):
    _load()
    await _penalty_sweeper()
    yield


app = FastAPI(title="电商规则合规助手", version="5.1", lifespan=lifespan)

# CORS 白名单不能写死 127.0.0.1:8000：容器/反代/域名上线后，
# 写死的白名单会让前端拿不到响应，而错误信息是浏览器侧的
# "CORS policy"，排查时根本看不出是服务端配置问题。
CORS_ORIGINS = [o.strip() for o in os.getenv(
    "CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000"
).split(",") if o.strip()]
app.add_middleware(CORSMiddleware,
                   allow_origins=CORS_ORIGINS,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

T_START = time.time()

class _NoCacheStatic(StaticFiles):
    """静态资源也要禁缓存。

    踩过的坑：api.js 是普通静态文件，浏览器按默认策略缓存了旧版；
    页面更新后调用新函数（如 senderTag）就报 ReferenceError，
    整个页面 JS 崩掉——「什么都加载不出来」，但接口全是 200，极难排查。
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp


if WEB.exists():
    app.mount("/assets", _NoCacheStatic(directory=str(WEB / "assets")), name="assets")


# ---------------------------------------------------------------- 错误留痕

LOG_DIR = ROOT / "logs"
_err_seq = 0


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """未捕获异常统一处理：浏览器拿到带编号的 JSON，服务端留下完整堆栈。

    为什么要自己做：默认行为是返回一行 "Internal Server Error"，
    前端只能弹"服务异常"，排查时既不知道哪一行、也没有请求上下文。
    编号能让用户报错时说清是哪一次，日志能直接定位。
    """
    global _err_seq
    _err_seq += 1
    eid = f"E{int(time.time()) % 100000}-{_err_seq}"
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with open(LOG_DIR / "error.log", "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 70}\n[{eid}] {time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"{request.method} {request.url.path}\n")
            import traceback
            f.write(traceback.format_exc())
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务内部错误，请把错误编号 {eid} 告诉管理员", "error_id": eid})


# ---------------------------------------------------------------- 启动

def _load():
    t0 = time.time()
    db_schema.ensure_ready()          # 建表 + 种子账号，可重复执行
    cache.init_redis()

    docs = common.load_clauses()
    vindex = get_vector_index()
    if not vindex.load():
        print("[startup] 向量索引缺失，降级为 BM25 单通道")
    else:
        # 预热 BGE-M3：向量库只加载了矩阵，编码器是懒加载的。
        # 不预热的话第一个提问要替 3~5 秒的模型加载买单（实测首问 9~12 秒）。
        try:
            vindex.encode(["预热"])
        except Exception as e:
            print("[startup] 编码器预热失败:", str(e)[:80])
    reranker = Reranker(enabled=RERANK_ON)
    if RERANK_ON:
        reranker.model                # 预热，别让第一个用户替加载买单
    index = HybridRetriever(BM25(docs), vindex, reranker=reranker)
    llm = get_llm()
    if llm.available():
        llm.client

    qa.init(docs, index, vindex, reranker, llm)
    _ensure_log_columns()

    # 智能客服：建 FAQ 索引 → 灌演示会话（只在库为空时）
    api_cs.boot()
    shop = common.fetch_all("SELECT id FROM t_shop ORDER BY id LIMIT 1")
    demo_n = cs.seed_demo(shop[0]["id"] if shop else None)
    faq_stats = api_cs.faq.index().stats()

    # 平台监管：店铺信用档案 + 演示纠纷
    api_gov.boot()
    gov_stats = api_gov.gov.metrics()
    # metrics() 内部会先做一次罚单到期回收，所以这里不用再显式调 expire_due()。
    # 上次停机期间到期的罚单，启动瞬间就已经被收掉、店铺状态恢复。

    print(f"[startup] {cache.rule_version()} 条款 {len(docs)} 条 | 后端 {common.backend()}/"
          f"{vindex.stats().get('backend')} | Redis={'on' if cache.RC else 'off'} | "
          f"精排={'on' if RERANK_ON else 'off'} | "
          f"大模型={llm.model if llm.available() else 'off(模板)'} | "
          f"{round(time.time() - t0, 1)}s")
    print(f"[startup] 智能客服：FAQ {faq_stats['faqs']} 条 | 阈值 {faq_stats['threshold']} | "
          f"演示会话 {demo_n} 个 | 四级漏斗 Redis→BM25→LLM→人工")
    print(f"[startup] 平台监管：纠纷 {gov_stats['disputes']} 单 | 待仲裁 "
          f"{gov_stats['pending_human']} | AI 自动结案率 "
          f"{round(gov_stats['ai_auto_rate'] * 100)}% | 自动裁决线 {gov_stats['threshold']}")


# 罚单到期巡检间隔（秒）。默认 10 分钟：处罚到期是「天」级的事，扫太勤只是白费查询；
# 但也不能一小时一次，否则演示时「停业转正常」会显得系统没反应。
PENALTY_SWEEP_SEC = int(os.getenv("PENALTY_SWEEP_SEC", "600"))


async def _penalty_sweeper():
    """后台巡检：把到期罚单标记 expired，并恢复店铺状态。

    shop_status() 已经在读取路径上做了自愈，为什么还要定时器：
    自愈只在「有人来读这家店」时触发。一家停业的店没人访问时，
    t_shop_credit.status 会一直停在 suspended，而监管报表、排行榜、批量导出
    这些不走 shop_status 的地方读到的仍是过期状态。
    读取自愈负责正确性，定时巡检负责整库一致性，两者都要有。
    """
    async def loop():
        while True:
            await asyncio.sleep(max(60, PENALTY_SWEEP_SEC))
            try:
                r = await asyncio.to_thread(gov_svc.expire_due)
                if r["expired"]:
                    print(f"[sweep] 罚单到期 {r['expired']} 张，恢复 {len(r['recovered'])} 家")
            except Exception as e:
                print("[sweep] 巡检失败:", str(e)[:100])

    asyncio.create_task(loop())


def _ensure_log_columns():
    """老库（v4 之前）的 t_qa_log 没有 generator / llm_ms 两列。"""
    for sql in ("ALTER TABLE t_qa_log ADD COLUMN generator VARCHAR(64)",
                "ALTER TABLE t_qa_log ADD COLUMN llm_ms DECIMAL(10,1)"):
        try:
            common.execute(sql)
        except Exception:
            pass


# ---------------------------------------------------------------- 路由挂载

app.include_router(api_auth.router)
app.include_router(api_chat.router)
app.include_router(api_settings.router)
app.include_router(api_rules.router)
app.include_router(api_admin.router)
app.include_router(api_cs.router)
app.include_router(api_gov.router)
app.include_router(api_orders.router)


# ---------------------------------------------------------------- 页面

def _page(name: str) -> FileResponse:
    p = WEB / name
    if not p.exists():
        raise HTTPException(500, f"页面缺失: {name}")
    # 页面也要禁缓存，否则改了 HTML 用户还在看旧的
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


def _home(u: dict) -> str:
    """各角色的首页。

    「工作台」已并入各端的咨询窗口：买家/商家/运营都落在 /cs（各自板块），
    管理员没有咨询功能、落在 /admin。四个端不再共用同一个 chat 工作台。
    """
    return "/admin" if u["role"] == "admin" else "/cs"


@app.get("/")
def root(request: Request):
    u = sec.optional_user(request)
    if u:
        return RedirectResponse(_home(u), status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def page_login(request: Request):
    u = sec.optional_user(request)
    if u:
        return RedirectResponse(_home(u), status_code=302)
    return _page("login.html")


@app.get("/app")
def page_app(request: Request):
    """独立工作台已并入各端咨询窗口，这个入口统一 302 回各端首页。"""
    u = sec.optional_user(request)
    if not u:
        return RedirectResponse("/login?next=/app", status_code=302)
    return RedirectResponse(_home(u), status_code=302)


@app.get("/rules")
def page_rules(request: Request):
    u = sec.optional_user(request)
    if not u:
        return RedirectResponse("/login?next=/rules", status_code=302)
    # 知识库只服务商家（规则咨询）与运营（治理口径）；买家和管理员没有这个功能。
    if u["role"] not in ("merchant", "ops"):
        return RedirectResponse(_home(u), status_code=302)
    return _page("rules.html")


@app.get("/orders")
def page_orders(request: Request):
    """买家的订单中心：订单列表 / 历史订单 / 申请退货退款 / 发起投诉。"""
    u = sec.optional_user(request)
    if not u:
        return RedirectResponse("/login?next=/orders", status_code=302)
    return _page("orders.html")


@app.get("/dashboard")
def page_dashboard(request: Request):
    """商家的经营看板：销售额趋势、订单、风险与纠纷（独立成页，图表看得清）。"""
    u = sec.optional_user(request)
    if not u:
        return RedirectResponse("/login?next=/dashboard", status_code=302)
    # 经营看板是商家自己的生意，运营/管理员/买家没有这个功能。
    if u["role"] != "merchant":
        return RedirectResponse(_home(u), status_code=302)
    return _page("dashboard.html")


@app.get("/settings")
def page_settings(request: Request):
    if not sec.optional_user(request):
        return RedirectResponse("/login?next=/settings", status_code=302)
    return _page("settings.html")


@app.get("/service")
def page_service(request: Request):
    """客服工作台（坐席侧）：商家自己的客服、平台运营的全平台巡检。

    买家不该进来——这里能看到的是「商家怎么处理别人」，不是「我的咨询」。
    买家的入口是 /cs（对话）和 /dispute（投诉）。
    """
    u = sec.optional_user(request)
    if not u:
        return RedirectResponse("/login?next=/service", status_code=302)
    if u["role"] not in ("merchant", "ops", "admin"):
        return RedirectResponse("/cs?denied=service", status_code=302)
    return _page("service.html")


@app.get("/cs")
def page_cs(request: Request):
    """咨询窗口。三个角色各进各的板块：买家问店铺、商家问平台规则、运营问治理口径。
    管理员没有咨询功能——不参与业务，不能和运营共用同一个治理界面。"""
    u = sec.optional_user(request)
    if not u:
        return RedirectResponse("/login?next=/cs", status_code=302)
    if u["role"] == "admin":
        return RedirectResponse("/admin", status_code=302)
    return _page("cs.html")


@app.get("/dispute")
def page_dispute(request: Request):
    """纠纷中心（买家投诉 + 商家答辩 + 双方申诉）。管理员不参与，回系统管理。"""
    u = sec.optional_user(request)
    if not u:
        return RedirectResponse("/login?next=/dispute", status_code=302)
    if u["role"] == "admin":
        return RedirectResponse("/admin", status_code=302)
    return _page("dispute.html")


@app.get("/platform")
def page_platform(request: Request):
    """平台监管工作台（仲裁 + 处罚 + 申诉 + 店铺监管），只属于平台运营。"""
    u = sec.optional_user(request)
    if not u:
        return RedirectResponse("/login?next=/platform", status_code=302)
    if u["role"] != "ops":
        return RedirectResponse(_home(u), status_code=302)
    return _page("platform.html")


@app.get("/admin")
def page_admin(request: Request):
    u = sec.optional_user(request)
    if not u:
        return RedirectResponse("/login?next=/admin", status_code=302)
    if u["role"] != "admin":
        # 非管理员不是"看不到入口"，是服务端直接拦住
        return RedirectResponse(_home(u), status_code=302)
    return _page("admin.html")


@app.get("/legacy")
def page_legacy():
    return _page("legacy.html")


@app.get("/demo")
def page_demo():
    """独立演示页：单文件 HTML，不需要登录，也不用前端构建。
    放在 static/ 下是为了让它能脱离服务直接双击打开做截图（接口地址可在页内改）。"""
    p = ROOT / "static" / "index.html"
    if not p.exists():
        raise HTTPException(500, "演示页缺失: static/index.html")
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- 兼容旧接口

class Ask(BaseModel):
    query: str
    session_id: str | None = None


@app.post("/api/ask")
def ask_legacy(req: Ask, request: Request):
    """早期单页 Demo 用的接口，保留但不做鉴权隔离（只有 /legacy 在用）。

    新前端请用 /api/chat/ask —— 那边有会话、历史、数据隔离。
    """
    q = req.query.strip()
    if not q:
        return JSONResponse({"error": "空问题"}, status_code=400)
    if API_KEY and request.headers.get("x-api-key", "") != API_KEY:
        raise HTTPException(status_code=401, detail="X-API-Key 无效")
    res = qa.answer(q)
    qa.log_qa(req.session_id or "legacy", None, res)
    res["cache_hit"] = False
    return res


# ---------------------------------------------------------------- 演示页 API

class DemoAsk(BaseModel):
    query: str


def _demo_ticket(question: str, res: dict) -> str:
    """L3 转人工时落一条**真实**工单，返回工单号。

    为什么要有这个：演示页如果不能给出工单号，"转人工"就只是一句客气话 ——
    用户不知道找谁、运营台也看不到这一单。所以这里真的建会话 + 真的建工单，
    /service 坐席台能看到，状态流转也走同一套。
    """
    try:
        import uuid as _uuid
        sid = "D" + time.strftime("%y%m%d%H%M%S") + _uuid.uuid4().hex[:6]
        shop = common.fetch_all("SELECT id FROM t_shop ORDER BY id LIMIT 1")
        common.insert(
            """INSERT INTO t_cs_session (id, shop_id, buyer_id, buyer_name, channel,
                   scope, status, last_msg, msg_count, handoff, created_at, updated_at)
               VALUES (%s,%s,NULL,%s,'demo','shop','bot',%s,1,1,%s,%s)""",
            (sid, shop[0]["id"] if shop else None, "演示访客",
             (question or "")[:200], db_schema.now(), db_schema.now()))
        tid = cs._ticket(sid, "high_risk", question, res.get("conclusion") or "", priority=2)
        return f"T{int(tid):06d}"
    except Exception as e:
        print("[demo] 工单创建失败:", str(e)[:120])
        return ""


@app.get("/api/demo/stats")
def demo_stats():
    """演示页首屏要的三个数：规则库版本、条款数、FAQ 条数。免鉴权、只有计数。"""
    def one(sql, args=()):
        try:
            r = common.fetch_all(sql, args)
            return int(list(r[0].values())[0]) if r else 0
        except Exception:
            return 0
    return {
        "rule_version": cache.rule_version(),
        "service_version": app.version,
        "clauses": one("SELECT COUNT(*) FROM t_clause"),
        "faqs": one("SELECT COUNT(*) FROM t_faq WHERE status='enabled'"),
        "shops": one("SELECT COUNT(*) FROM t_shop"),
        "llm": (qa.RT.llm.model if qa.RT.llm and qa.RT.llm.available() else None),
    }


@app.post("/api/demo/ask")
def demo_ask(req: DemoAsk, request: Request):
    """演示页一问一答。不复用 /api/ask 的原因：那支返回的是内部结构，
    且不做工单；演示页需要"L3 给出真实工单号"这一件事，仅此而已。

    只返回本次问答产生的内容，不碰任何他人数据；限流与 /api/ask 同口径。
    """
    q = (req.query or "").strip()
    if not q:
        raise HTTPException(400, "空问题")
    ip = (request.client.host if request.client else "anon")
    ok, n = cache.rate_ok(f"demo:{ip}", RATE_LIMIT)
    if not ok:
        raise HTTPException(429, f"请求过于频繁（{n}/{RATE_LIMIT} 每分钟）")

    # 走和 /api/chat 完全一样的缓存口径：key 带规则库版本号。
    # 演示页底部那个"缓存命中"因此是真的 —— 同一个问题第二次点，确实没重算。
    import hashlib as _hl
    _ver = cache.rule_version()
    _mt = (qa.RT.llm.model or "template") if qa.RT.llm else "template"
    ck = (f"rr:{_ver}:demo:{_hl.md5(_mt.encode()).hexdigest()[:8]}:"
          f"{_hl.md5(q.encode()).hexdigest()}")
    t0 = time.time()
    res = cache.get(ck)
    cache_hit = res is not None
    if res is None:
        res = qa.answer(q)
        cache.set(ck, res, ttl=600)
        qa.log_qa("demo", None, res)
    else:
        res = {**res, "latency_ms": int((time.time() - t0) * 1000)}

    out = {
        "cache_hit": cache_hit,
        "query": q,
        "risk_level": res.get("risk_level"),
        "conclusion": res.get("conclusion") or "",
        "notice": res.get("notice") or "",
        "handoff": bool(res.get("handoff")),
        "handoff_reason": res.get("handoff_reason") or "",
        "timeouts": res.get("timeouts") or [],
        "generator": res.get("generator"),
        "latency_ms": res.get("latency_ms"),
        "rule_version": cache.rule_version(),
        "timeouts_rule": res.get("timeouts"),
        "clauses": [{
            "doc_name": c.get("doc_name"), "clause_no": c.get("clause_no"),
            "clause_path": c.get("clause_path"), "content": c.get("content"),
            "effective_from": c.get("effective_from"), "source_url": c.get("source_url"),
        } for c in (res.get("clauses") or [])],
    }
    out["ticket_no"] = _demo_ticket(q, res) if out["handoff"] else ""
    return out


# ---------------------------------------------------------------- 运维

@app.get("/api/health")
def health():
    """免鉴权：监控系统要能探活，不能先登录。只暴露状态不暴露数据。"""
    # rule_version 必须取维护中的那个号，不能取模块常量：
    # 健康检查是外部监控唯一的读数来源，它报 v5 而缓存实际是 v8 的话，
    # 排查"为什么规则更新了没生效"会从错误的前提出发。
    out = {"service": "up", "uptime_s": int(time.time() - T_START),
           "version": app.version, "rule_version": cache.rule_version()}
    t = time.time()
    try:
        common.fetch_all("SELECT 1 AS x")
        out["db"] = {"backend": common.backend(), "ok": True,
                     "ms": round((time.time() - t) * 1000, 1)}
    except Exception as e:
        out["db"] = {"backend": common.backend(), "ok": False, "error": str(e)[:80]}
    out["redis"] = cache.stats()
    out["vector"] = qa.RT.vindex.stats() if qa.RT.vindex else {}
    out["rerank"] = qa.RT.reranker.stats() if qa.RT.reranker else {"enabled": False}
    out["llm"] = qa.RT.llm.stats() if qa.RT.llm else {"available": False}
    try:
        out["cs"] = {"faq": api_cs.faq.index().stats(),
                     "tickets_pending": common.fetch_all(
                         "SELECT COUNT(*) AS n FROM t_ticket WHERE status='pending'")[0]["n"]}
    except Exception as e:
        out["cs"] = {"ok": False, "error": str(e)[:80]}
    try:
        m = gov_svc.metrics()
        out["gov"] = {"disputes": m["disputes"], "pending_human": m["pending_human"],
                      "ai_auto_rate": m["ai_auto_rate"],
                      "appeals_pending": m["appeals"].get("human_review", 0),
                      "shops": m["shops"]}
    except Exception as e:
        out["gov"] = {"ok": False, "error": str(e)[:80]}
    return out


@app.get("/api/stats")
def stats(user: dict = Depends(sec.current_user)):
    orders = common.fetch_all("SELECT COUNT(*) AS n FROM t_order")[0]["n"]
    qa_row = common.fetch_all(
        "SELECT COUNT(*) AS n, ROUND(AVG(latency_ms)) AS avg_ms FROM t_qa_log")[0]
    return {
        "clauses": len(qa.RT.docs), "orders": orders,
        # 版本号唯一来源是 cache.rule_version()（读 Redis/文件），
        # 不能再返回写死的 RULE_VERSION —— 否则 /api/stats 显示 v5、/api/health 显示 v8。
        "rule_version": cache.rule_version(),
        "runtime": qa.RT.stats(),
        "qa_log": qa_row,
    }


@app.get("/api/stats/merchant")
def merchant_stats(user: dict = Depends(sec.current_user)):
    """商家经营看板：本周/本月/本年销售额、近 14 日趋势、风险与纠纷。

    只按当前账号的 shop_id 收口，商家看不到别家；运营/管理员没有 shop_id 直接 400。
    """
    sid = user.get("shop_id")
    if not sid:
        raise HTTPException(400, "仅商家账号可查看经营看板")
    shop = common.fetch_all("SELECT name FROM t_shop WHERE id=%s", (sid,))
    if not shop:
        raise HTTPException(404, "店铺不存在")
    name = shop[0]["name"]
    from datetime import datetime, timedelta
    now = datetime.now().replace(microsecond=0)

    def sum_since(ts):
        r = common.fetch_all(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS s FROM t_order "
            "WHERE shop_name=%s AND status NOT IN ('待付款','已关闭') AND paid_at>=%s",
            (name, ts.strftime("%Y-%m-%d %H:%M:%S")))[0]
        return {"orders": int(r["n"]), "amount": round(float(r["s"]), 2)}

    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0)
    since = (now - timedelta(days=13)).replace(hour=0, minute=0, second=0)
    rows = common.fetch_all(
        "SELECT substr(paid_at,1,10) AS d, COALESCE(SUM(amount),0) AS s FROM t_order "
        "WHERE shop_name=%s AND status NOT IN ('待付款','已关闭') AND paid_at>=%s "
        "GROUP BY d ORDER BY d", (name, since.strftime("%Y-%m-%d %H:%M:%S")))
    daily = {r["d"]: round(float(r["s"]), 2) for r in rows}
    trend = []
    for i in range(14):
        day = since + timedelta(days=i)
        trend.append({"date": day.strftime("%m-%d"),
                      "amount": daily.get(day.strftime("%Y-%m-%d"), 0)})
    try:
        fake = int(common.fetch_all(
            "SELECT COUNT(*) AS n FROM t_order WHERE shop_name=%s AND picked_at IS NOT NULL "
            "AND (julianday(picked_at)-julianday(shipped_at))*24>24", (name,))[0]["n"])
    except Exception:
        fake = 0
    disp = int(common.fetch_all("SELECT COUNT(*) AS n FROM t_dispute WHERE shop_id=%s", (sid,))[0]["n"])
    disp_open = int(common.fetch_all(
        "SELECT COUNT(*) AS n FROM t_dispute WHERE shop_id=%s AND status NOT IN ('resolved','closed')",
        (sid,))[0]["n"])
    return {"shop": name, "week": sum_since(week_start), "month": sum_since(month_start),
            "year": sum_since(year_start), "trend": trend,
            "risk": {"fake_ship": fake},
            "disputes": {"total": disp, "open": disp_open}}


@app.get("/api/eval")
def eval_report(user: dict = Depends(sec.current_user)):
    p = ROOT / "data" / "processed" / "eval_latest.json"
    if not p.exists():
        return {"available": False}
    import json
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/order/{order_no}")
def order(order_no: str, user: dict = Depends(sec.current_user)):
    rows = common.fetch_all("SELECT * FROM t_order WHERE order_no=%s", (order_no,))
    if not rows:
        return {}
    # 归属校验：商家只能看自己店的订单；运营/管理员可看全平台。
    if user["role"] == "merchant":
        shop = common.fetch_all("SELECT name FROM t_shop WHERE id=%s", (user.get("shop_id"),))
        if not shop or rows[0].get("shop_name") != shop[0]["name"]:
            raise HTTPException(404, "订单不存在")
    return rows[0]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
