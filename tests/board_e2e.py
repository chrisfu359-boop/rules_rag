"""三个咨询板块的端到端验收。

要证明的不是"页面长得不一样"，而是四件事：

  1. 角色进的是不同板块，标的是不同知识域（买家→店铺 / 商家→规则 / 运营→治理）
  2. 同一句话在三个域得到不同答案，**且缓存不串**（Redis key 带知识域）
  3. 快捷问题从本域知识库生成，点下去必须命中；转人工那条必须落工单
  4. 助手说的话和裁决引擎的数值一致（FAQ 里的"扣 X 分"必须能在条款表里找到）

第 4 条是这套设计的地基：如果规则助手说"售假扣 12 分"，而系统判 24 分，
那这个助手不是帮忙，是在制造投诉。

用法（项目根目录）：
    python tests/board_e2e.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.getenv("BOARD_TEST_PORT", "8016"))
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
            if httpx.get(f"{BASE}/api/health", timeout=2, trust_env=False).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _clean_redis() -> None:
    """不清缓存的话，"首次提问命中 FAQ"会变成"命中 Redis"，断言时灵时不灵。"""
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


def login(username: str, pwd: str) -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
    r = c.post("/api/auth/login", json={"username": username, "password": pwd})
    if r.status_code != 200:
        raise SystemExit(f"登录失败 {username}: {r.status_code} {r.text[:80]}")
    return c


def say(c: httpx.Client, sid: str, text: str) -> dict:
    return c.post(f"/api/cs/sessions/{sid}/messages", json={"text": text}).json()


def run() -> None:
    buyer = login("lichen", "123456")
    mer = login("fengxia", "123456")
    ops = login("ops", "Ops@12345")

    # ---------------------------------------------------------------- 1
    print("\n[1] 三个角色进三个板块")
    b_buyer = buyer.get("/api/cs/board").json()
    b_mer = mer.get("/api/cs/board").json()
    b_ops = ops.get("/api/cs/board").json()
    check("买家板块=店铺客服", b_buyer["scope"] == "shop" and b_buyer["audience"] == "buyer",
          f"{b_buyer['label']} / {b_buyer['audience']}")
    check("商家板块=平台规则助手",
          b_mer["scope"] == "rule" and b_mer["audience"] == "merchant",
          f"{b_mer['label']} / {b_mer['audience']}")
    check("运营板块=治理规范助手",
          b_ops["scope"] == "gov" and b_ops["audience"] == "ops",
          f"{b_ops['label']} / {b_ops['audience']}")
    check("三个板块的知识源各不同",
          len({b_buyer["audience_label"], b_mer["audience_label"],
               b_ops["audience_label"]}) == 3,
          f"{b_buyer['audience_label']} / {b_mer['audience_label']} / {b_ops['audience_label']}")
    check("买家越权要规则板块被回落",
          buyer.get("/api/cs/board", params={"scope": "rule"}).json()["scope"] == "shop")
    check("商家预览店铺客服被允许",
          mer.get("/api/cs/board", params={"scope": "shop"}).json()["scope"] == "shop")

    # 板块分开的第二层：窗口里能点哪些动作，必须由板块决定。
    # 以前动作分支写在前端 if/else 里，治理板块落进了买家那一支，
    # 运营在右上角看到「平台介入」——点下去是替别人投诉。
    check("买家板块才有平台介入",
          b_buyer["ui"]["can_escalate"] and
          any(a["kind"] == "escalate" for a in b_buyer["ui"]["actions"]),
          str([a["label"] for a in b_buyer["ui"]["actions"]]))
    check("商家板块没有平台介入",
          not b_mer["ui"]["can_escalate"],
          str([a["label"] for a in b_mer["ui"]["actions"]]))
    check("运营板块没有平台介入，只有仲裁台/规则库",
          [a["label"] for a in b_ops["ui"]["actions"]] == ["仲裁台", "规则库"],
          str([a["label"] for a in b_ops["ui"]["actions"]]))
    check("运营预览店铺客服也不给平台介入",
          not ops.get("/api/cs/board", params={"scope": "shop"}).json()["ui"]["can_escalate"])
    check("三个板块的引导语互不相同",
          len({b_buyer["ui"]["greet"], b_mer["ui"]["greet"], b_ops["ui"]["greet"]}) == 3)

    # ---------------------------------------------------------------- 2
    print("\n[2] 三个板块各自的回答")
    s_buyer = buyer.post("/api/cs/sessions", json={"shop_id": 2}).json()
    s_mer = mer.post("/api/cs/sessions", json={}).json()
    s_ops = ops.post("/api/cs/sessions", json={}).json()
    check("买家会话落店铺板块", s_buyer["scope"] == "shop" and s_buyer["shop_id"] == 2)
    check("商家会话不挂店铺", s_mer["scope"] == "rule" and s_mer["shop_id"] is None,
          f"scope={s_mer['scope']} shop_id={s_mer['shop_id']}")
    check("运营会话不挂店铺", s_ops["scope"] == "gov" and s_ops["shop_id"] is None)

    a = say(buyer, s_buyer["id"], "什么时候发货")
    check("买家问发货命中店铺知识库", a["source"] == "faq" and "48 小时" in a["reply"],
          f"{a['source']} {a.get('score')}")

    m1 = say(mer, s_mer["id"], "售假扣多少分")
    check("商家问售假命中规则库", m1["source"] == "faq", f"{m1['source']} {m1.get('score')}")
    check("商家答案带条款号与分值", "3.1" in m1["reply"] and "24 分" in m1["reply"],
          m1["reply"][:46])

    o1 = say(ops, s_ops["id"], "七天无理由的法定范围")
    check("运营问法条命中规范库", o1["source"] == "faq", f"{o1['source']}")
    check("运营答案带法律条号", "第 25 条" in o1["reply"], o1["reply"][:46])

    # ---------------------------------------------------------------- 3
    print("\n[3] 同一句话，三个域三个答案（缓存不许串）")
    q = "七天无理由运费谁承担"
    m2 = say(mer, s_mer["id"], q)
    o2 = say(ops, s_ops["id"], q)
    check("商家侧命中", m2["source"] == "faq", f"{m2['source']} {m2.get('score')}")
    check("运营侧命中", o2["source"] == "faq", f"{o2['source']} {o2.get('score')}")
    check("两侧答案不同（缓存 key 带了知识域）",
          m2["reply"] != o2["reply"],
          f"商家「{m2['reply'][:22]}」 vs 运营「{o2['reply'][:22]}」")
    check("商家侧讲操作口径", "商家承担" in m2["reply"] or "消费者承担" in m2["reply"])
    check("运营侧讲条文适用", "第 25 条" in o2["reply"], o2["reply"][:40])

    # ---------------------------------------------------------------- 4
    print("\n[4] 快捷问题必须真能命中")
    for label, board, client in (("买家", b_buyer, buyer), ("商家", b_mer, mer),
                                 ("运营", b_ops, ops)):
        chips = board.get("chips") or []
        check(f"{label}板块有快捷问题", len(chips) >= 5, f"{len(chips)} 条")
        s = client.post("/api/cs/sessions", json={
            "shop_id": 2} if board["scope"] == "shop" else {}).json()
        miss = []
        for c in chips:
            # handoff 是"转人工"示例，case 是"挑一单"入口，两者都不是问句
            if c.get("handoff") or c.get("kind") == "case":
                continue
            r = say(client, s["id"], c["q"])
            if r["source"] not in ("faq", "redis"):
                miss.append(f"{c['q']}→{r['source']}")
        check(f"{label}板块快捷问题全部命中", not miss, "；".join(miss) or "全部命中")

    # 治理板块最后一格不是问句而是入口：以前写「这个案子怎么判」，
    # 「这个」没有指代对象，点下去又必然被 case_judge 拦成转人工。
    case_chips = [c for c in b_ops["chips"] if c.get("kind") == "case"]
    check("治理板块的个案入口是选单而不是问句",
          len(case_chips) == 1 and not any(w in case_chips[0]["q"] for w in ("这个", "这单")),
          str([c["q"] for c in b_ops["chips"]]))
    check("买家/商家板块没有带单核查入口",
          not [c for c in b_buyer["chips"] + b_mer["chips"] if c.get("kind") == "case"])

    print("\n[4b] 带单核查：事实 + 条款，不给结论")
    check("商家拿不到案件列表", mer.get("/api/cs/cases").status_code == 403)
    check("买家拿不到案件列表", buyer.get("/api/cs/cases").status_code == 403)
    cases = ops.get("/api/cs/cases").json()["items"]
    check("运营能看到候选案件", bool(cases), f"{len(cases)} 条")
    if cases:
        one = cases[0]
        r = ops.post(f"/api/cs/sessions/{s_ops['id']}/case",
                     json={"dispute_id": one["id"]}).json()
        check("带单核查摊开订单事实",
              "【订单】" in r["reply"] and one["order_no"] in r["reply"], r["reply"][:44])
        check("带单核查给条款原文", "【适用条款】" in r["reply"] and "《" in r["reply"])
        check("带单核查不给结论",
              "不替仲裁员下结论" in r["reply"] and "仲裁台" in r["reply"])
        check("带单核查带相关条款检索", "【相关条款】" in r["reply"]
              and "条现行条款中检索" in r["reply"],
              r["reply"][r["reply"].find("【相关条款】"):][:40])
        check("带单核查写进会话记录", r["source"] == "case", r["source"])
    r = mer.post(f"/api/cs/sessions/{s_mer['id']}/case", json={"dispute_id": 1})
    check("规则板块不能带单核查", r.status_code in (400, 403), f"HTTP {r.status_code}")

    # ---------------------------------------------------------------- 5
    print("\n[5] 该拦的必须拦")
    r = say(mer, s_mer["id"], "我的店被扣了 24 分，凭什么")
    check("商家问个案 → 转规则运营",
          r["source"] == "rule" and r["handoff"] and bool(r["ticket_id"]),
          f"{r['source']} ticket={r.get('ticket_id')}")
    check("个案话术不编造分数", "24 分" not in r["reply"], r["reply"][:46])

    r = say(ops, s_ops["id"], "这个案子怎么判")
    check("运营要求判个案 → 拒绝并指路仲裁台",
          r["handoff"] and "仲裁台" in r["reply"], r["reply"][:46])

    r = mer.post(f"/api/cs/sessions/{s_mer['id']}/escalate", json={})
    check("规则会话不能转平台介入", r.status_code == 400, f"HTTP {r.status_code}")

    r = buyer.get(f"/api/cs/sessions/{s_mer['id']}")
    check("买家看不到别人的规则会话", r.status_code == 404, f"HTTP {r.status_code}")

    # PATCH 只传 answer：以前 question 会被当成空串打成 400，
    # 而且 keywords/aliases 被静默清空。这条是回归用例。
    # v8 起商家改不了平台通用条目（全局基线的改动会影响所有店铺），
    # 所以用例改成"先在本店建一条，再只 PATCH answer"，同时守住
    # 「改平台条目必须 403」这条新规矩。
    made = mer.post("/api/cs/faq", json={
        "question": "PATCH 回归用例问法", "answer": "原始答案",
        "category": "通用", "keywords": "回归,用例", "aliases": "只改答案试试"}).json()
    fid = made["id"]
    r = mer.patch(f"/api/cs/faq/{fid}", json={"answer": "改一个字试试"})
    check("只改 answer 的 PATCH 不报错", r.status_code == 200, f"HTTP {r.status_code}")
    after = [x for x in mer.get("/api/cs/faq", params={"limit": 300}).json()["items"]
             if x["id"] == fid][0]
    check("PATCH 不会清空未传的字段",
          after["question"] == "PATCH 回归用例问法" and after["keywords"] == "回归,用例",
          f"q={after['question'][:12]} kw={after['keywords'][:16]}")
    gid = ops.get("/api/cs/faq", params={"scope": "global", "limit": 5}).json()["items"][0]["id"]
    r = mer.patch(f"/api/cs/faq/{gid}", json={"answer": "改全平台的基线"})
    check("商家改平台通用条目 → 403", r.status_code == 403, f"HTTP {r.status_code}")

    # 商家不能碰规则库：找一条 merchant 域条目
    faqs = ops.get("/api/cs/faq", params={"audience": "merchant"}).json()["items"]
    rid = faqs[0]["id"]
    r = mer.patch(f"/api/cs/faq/{rid}", json={"answer": "售假扣 1 分"})
    check("商家改平台规则库 → 403", r.status_code == 403, f"HTTP {r.status_code}")
    r = mer.get("/api/cs/faq", params={"audience": "merchant"}).json()
    check("商家列表被强制回买家域", r["audience"] == "buyer", r["audience"])

    # ---------------------------------------------------------------- 6
    print("\n[6] 会话归属与队列隔离")
    lst = mer.get("/api/cs/sessions").json()["items"]
    scopes = {x["scope"] for x in lst}
    check("坐席队列只有店铺会话", scopes <= {"shop"}, str(scopes))
    check("商家的规则会话不在坐席队列",
          all(x["id"] != s_mer["id"] for x in lst))

    allq = ops.get("/api/cs/sessions", params={"scope": "all"}).json()["items"]
    check("运营可跨板块查看", any(x["scope"] == "gov" for x in allq),
          str({x["scope"] for x in allq}))

    # ---------------------------------------------------------------- 7
    print("\n[7] 助手口径与裁决引擎一致")
    meta = ops.get("/api/gov/meta").json()
    allowed = {float(t["points"]) for t in meta["types"] if t.get("points")}
    allowed |= {float(n["points"]) for n in meta["nodes"]}
    bad = []
    for aud in ("merchant", "ops"):
        items = ops.get("/api/cs/faq", params={"audience": aud, "limit": 300}).json()["items"]
        for it in items:
            for num in re.findall(r"(\d+(?:\.\d+)?)\s*分", it["answer"]):
                if float(num) not in allowed:
                    bad.append(f"{aud}#{it['id']}「{it['question']}」→ {num} 分")
    check("规则/规范库出现的分值都能在条款表里找到", not bad, "；".join(bad[:3]))

    for c in (buyer, mer, ops):
        c.close()


def main() -> int:
    env = {**os.environ, "LLM": "off", "DB_BACKEND": os.getenv("DB_BACKEND", "sqlite")}
    if env["DB_BACKEND"] == "sqlite":
        snap = ROOT / "data" / "_board_e2e.db"
        shutil.copy(ROOT / "data" / "rules.db", snap)
        env["SQLITE_PATH"] = str(snap)
    _clean_redis()
    (ROOT / "logs").mkdir(exist_ok=True)
    log = open(ROOT / "logs" / "board_e2e.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_up():
            print("服务未能在 90 秒内启动，见 logs/board_e2e.log")
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


if __name__ == "__main__":
    sys.exit(main())
