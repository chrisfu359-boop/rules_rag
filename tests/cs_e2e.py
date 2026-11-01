"""智能客服端到端验收：一次跑完四级漏斗 + 人工闭环 + 权限隔离。

为什么要起真实进程而不是用 TestClient：
  这次要验证的东西里有 Redis、有跨请求的会话状态、有"服务重启后索引还在不在"，
  这些在进程内 mock 出来的行为不可信。宁可多花 20 秒起一个真进程。
  踩过的教训：用测试桩跑出来的"通过"，比 bug 更危险。

用法（项目根目录）：
    python tests/cs_e2e.py            # 用 8010 端口，不影响正在跑的 8000
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.getenv("CS_TEST_PORT", "8010"))
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    return ok


def wait_up(timeout: int = 90) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(f"{BASE}/api/health", timeout=2, trust_env=False)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _clean_redis() -> None:
    """清掉上一次运行留在 Redis 里的答案。

    key 带规则库版本号（rr:cs:v{n}:q:{md5}），版本不变 key 就不变，于是第二次跑
    「首次提问」直接命中缓存，② 层的断言时灵时不灵。测试不能靠运气通过。
    """
    try:
        import redis as _redis
        r = _redis.Redis(host=os.getenv("REDIS_HOST", "127.0.0.1"),
                         port=int(os.getenv("REDIS_PORT", "6379")),
                         password=os.getenv("REDIS_PASSWORD", "123456"),
                         socket_connect_timeout=1)
        for k in r.scan_iter("rr:cs:*"):
            r.delete(k)
    except Exception:
        pass


def main() -> int:
    env = {**os.environ, "LLM": "off", "DB_BACKEND": os.getenv("DB_BACKEND", "sqlite")}
    if env["DB_BACKEND"] == "sqlite":
        # 从演示库快照起步：验收会话和纠纷不要写进 data/rules.db
        snap = ROOT / "data" / "_cs_e2e.db"
        shutil.copy(ROOT / "data" / "rules.db", snap)
        env["SQLITE_PATH"] = str(snap)
    _clean_redis()
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    log = open(ROOT / "logs" / "cs_e2e.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_up():
            print("服务未能在 90 秒内启动，见 logs/cs_e2e.log")
            return 2
        run()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 56}\n通过 {len(PASS)}/{total}")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 0 if not FAIL else 1


def run() -> None:
    c = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
    print("\n[1] 登录与鉴权")
    r = c.post("/api/auth/login", json={"username": "fengxia", "password": "123456"})
    check("商家登录", r.status_code == 200, f"HTTP {r.status_code}")
    anon = httpx.Client(base_url=BASE, timeout=10, trust_env=False)
    check("未登录访问会话列表返回 401",
          anon.get("/api/cs/sessions").status_code == 401)

    print("\n[2] 四级漏斗")
    # 商家默认进的是「平台规则助手」，这里要测店铺客服，必须显式 scope=shop
    sid = c.post("/api/cs/sessions",
                 json={"buyer_name": "验收买家", "channel": "淘宝", "scope": "shop"}
                 ).json()["id"]

    def say(text):
        return c.post(f"/api/cs/sessions/{sid}/messages", json={"text": text}).json()

    a = say("什么时候发货")
    check("② 知识库直出", a["source"] == "faq" and a["score"] >= 0.88,
          f"source={a['source']} score={a['score']}")
    check("知识库答案内容正确", "48 小时" in a["reply"], a["reply"][:30])

    b = say("什么时候发货")
    check("① 二次提问命中 Redis 缓存", b["source"] == "redis", f"source={b['source']}")
    # 同机 localhost 上两者都是个位数到几十毫秒，卡太死会变成测网络抖动
    check("缓存命中不快于首次", b["latency_ms"] <= a["latency_ms"] + 30,
          f"首次 {a['latency_ms']}ms / 缓存 {b['latency_ms']}ms")

    d = say("我的退款什么时候能到账")
    check("③ 未达阈值走大模型/兜底", d["source"] == "template" and d["score"] < 0.88,
          f"source={d['source']} score={d['score']}")
    check("③/④ 兜底生成工单", bool(d["ticket_id"]), f"ticket={d['ticket_id']}")

    e = say("你们卖的什么破东西我要投诉")
    check("④ 投诉类强制转人工（不进检索）", e["source"] == "rule" and e["handoff"],
          f"source={e['source']}")

    f = say("转人工")
    check("④ 主动要求人工直接转接", f["source"] == "rule" and f["handoff"])

    print("\n[3] 人工接管闭环")
    c.post(f"/api/cs/sessions/{sid}/claim")
    sess = c.get(f"/api/cs/sessions/{sid}").json()["session"]
    check("接管后会话状态为 agent", sess["status"] == "agent", sess["status"])

    silent = say("在吗")
    check("接管后机器人闭嘴", silent["source"] == "human" and silent["reply"] == "",
          f"source={silent['source']}")

    rep = c.post(f"/api/cs/sessions/{sid}/reply", json={"text": "在的，我来帮您处理退款。"})
    check("坐席可以回复", rep.status_code == 200)
    msgs = c.get(f"/api/cs/sessions/{sid}").json()["messages"]
    check("买家能看到人工回复", any(m["role"] == "agent" for m in msgs))

    dist = c.post(f"/api/cs/sessions/{sid}/distill")
    check("人工回复可沉淀为知识库", dist.status_code == 200 and dist.json().get("faq_id"),
          str(dist.json())[:80])
    check("沉淀先进入待审（不直接生效）", dist.json().get("status") == "pending",
          f"status={dist.json().get('status')}")

    # 沉淀审核流：待审条目不进检索，ops 过审后才 enabled 进索引
    opc = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
    opc.post("/api/auth/login", json={"username": "ops", "password": "Ops@12345"})
    pend = opc.get("/api/cs/faq/pending").json()
    check("运营能看到待审队列", pend["total"] >= 1, f"total={pend['total']}")
    target = next((x for x in pend["items"] if x["id"] == dist.json()["faq_id"]), None)
    check("本次沉淀在待审队列中", target is not None)
    rv = opc.post(f"/api/cs/faq/{dist.json()['faq_id']}/review",
                  json={"approve": True, "note": "验收通过"})
    check("审核通过转 enabled", rv.status_code == 200 and rv.json()["status"] == "enabled",
          str(rv.json())[:80])
    rv2 = opc.post(f"/api/cs/faq/{dist.json()['faq_id']}/review",
                   json={"approve": True})
    check("重复审核被拦截", rv2.status_code == 400, f"HTTP {rv2.status_code}")
    # 商家无权看待审队列（只能 ops/admin）
    r_pend = c.get("/api/cs/faq/pending")
    check("商家不能访问待审队列", r_pend.status_code == 403, f"HTTP {r_pend.status_code}")
    opc.close()

    c.post(f"/api/cs/sessions/{sid}/release")
    check("结束会话后交还机器人",
          c.get(f"/api/cs/sessions/{sid}").json()["session"]["status"] == "bot")

    print("\n[4] 知识库管理与阈值调试")
    probe = c.post("/api/cs/faq/probe", json={"query": "什么时候发货"}).json()
    cand = probe["candidates"][0]
    check("调试器给出完整打分过程",
          probe["hit"] and cand["sim"] >= 0.88 and "cov" in cand and "sat" in cand,
          f"sim={cand['sim']} cov={cand['cov']}")
    check("阈值可在调试器里读到", probe["threshold"] == 0.88)

    newf = c.post("/api/cs/faq", json={
        "question": "支持白条分期吗",
        "answer": "支持京东白条 3 期免息，6 期和 12 期需手续费。",
        "category": "支付问题", "aliases": "白条能分期吗|分期怎么弄",
    }).json()
    check("新增知识条目成功", newf.get("ok") and newf.get("id"))
    check("商家新建条目先待审（不立即生效）", newf.get("status") == "pending",
          f"status={newf.get('status')}")
    v_after = newf["version"]

    hit2 = say("白条能分期吗")
    check("待审条目不进检索（未命中）", hit2["source"] != "faq",
          f"source={hit2['source']} score={hit2['score']}")

    # 运营手动建条目直接生效（有审核权），这是唯一"建了就进检索"的正规入口
    opc2 = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
    opc2.post("/api/auth/login", json={"username": "ops", "password": "Ops@12345"})
    newf_ops = opc2.post("/api/cs/faq", json={
        "question": "支持花呗分期吗",
        "answer": "支持花呗 3 期免息。",
        "category": "支付问题", "aliases": "花呗能分期吗",
    }).json()
    check("运营建条目直接生效", newf_ops.get("status") == "enabled",
          f"status={newf_ops.get('status')}")
    hit3 = say("花呗能分期吗")
    check("运营条目立即命中", hit3["source"] == "faq",
          f"source={hit3['source']} score={hit3['score']}")
    opc2.close()

    c.patch(f"/api/cs/faq/{newf['id']}", json={"answer": "支持白条分期，3 期免息。"})
    check("修改后版本号递增", c.get("/api/cs/faq").json()["version"] > 0)

    print("\n[5] 权限隔离")
    m2 = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
    m2.post("/api/auth/login", json={"username": "xumeng", "password": "123456"})
    r404 = m2.get(f"/api/cs/sessions/{sid}")
    check("另一商家访问他人会话返回 404（不是 403）", r404.status_code == 404,
          f"HTTP {r404.status_code}")
    r_faq = m2.delete(f"/api/cs/faq/{newf['id']}")
    check("普通商家不能下架知识条目", r_faq.status_code == 403, f"HTTP {r_faq.status_code}")

    print("\n[6] 指标")
    m = c.get("/api/cs/metrics").json()
    check("指标包含四级来源分布", "by_source" in m and isinstance(m["by_source"], dict))
    check("自动解决率可计算", 0 <= m["auto_resolve_rate"] <= 1,
          f"{m['auto_resolve_rate']}")
    print(f"     来源分布 {m['by_source']} | 自动解决率 "
          f"{m['auto_resolve_rate'] * 100:.1f}% | 待处理工单 {m['ticket_pending']}")

    print("\n[7] 会话转平台介入")
    buyer = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
    buyer.post("/api/auth/login", json={"username": "lichen", "password": "123456"})
    shops = buyer.get("/api/gov/meta").json()["shops"]
    shop = [x for x in shops if x["name"] == "梦境数码专营店"][0]
    bsid = buyer.post("/api/cs/sessions", json={"shop_id": shop["id"]}).json()["id"]
    for t in ("耳机收到就是坏的，开不了机", "售后一直没人处理，让我自己去检测"):
        buyer.post(f"/api/cs/sessions/{bsid}/messages", json={"text": t})

    pre = buyer.get(f"/api/cs/sessions/{bsid}/prefill").json()
    check("转介预填带出对话稿", "耳机" in pre["transcript"], pre["dtype"])

    r = buyer.post(f"/api/cs/sessions/{bsid}/escalate",
                   json={"claim_amount": 199}).json()
    check("买家会话一键转平台介入", bool(r.get("dispute_no")),
          f"{r.get('dispute_no')} / {r.get('status')} / 置信 {r.get('confidence')}")

    again = buyer.post(f"/api/cs/sessions/{bsid}/escalate", json={}).json()
    check("重复提交沿用原单", again.get("exists") is True and again.get("id") == r.get("id"),
          str(again.get("dispute_no")))

    s2 = buyer.get(f"/api/cs/sessions/{bsid}").json()
    check("会话状态转为 escalated", s2["session"]["status"] == "escalated",
          s2["session"]["status"])
    check("转介后不再显示转介入口", s2["can_escalate"] is False)

    tail = buyer.post(f"/api/cs/sessions/{bsid}/messages", json={"text": "怎么没人管"}).json()
    check("转介后机器人指路纠纷中心",
          tail["source"] == "rule" and "纠纷中心" in tail["reply"], tail["reply"][:30])

    dd = buyer.get(f"/api/gov/disputes/{r['id']}").json()
    check("仲裁台能读到来源会话", (dd.get("session") or {}).get("id") == bsid,
          f"{(dd.get('session') or {}).get('status_label')}")
    check("来源会话带完整对话",
          len((dd.get("session") or {}).get("messages") or []) >= 4)
    check("聊天记录自动成为证据",
          "聊天记录" in [e["k"] for e in dd["dispute"]["evidence"]],
          str([e["k"] for e in dd["dispute"]["evidence"]]))

    r_bad = c.post(f"/api/cs/sessions/{sid}/escalate", json={})
    check("商家不能把自己的会话转成投诉自己店", r_bad.status_code == 400,
          f"HTTP {r_bad.status_code} {(r_bad.text or '')[:40]}")

    c.close()
    m2.close()
    anon.close()
    buyer.close()


if __name__ == "__main__":
    sys.exit(main())
