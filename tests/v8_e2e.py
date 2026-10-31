"""v8 端到端验收：知识按店铺隔离 + 订单商品明细/运单号。

这两件事看起来不相干，其实是同一个问题的两面：
**平台凭什么结论？** 靠两样东西——每家店自己的承诺，和每张订单的客观事实。

  A. 知识按店铺隔离
     以前 74 条买家知识全局共享，"什么时候发货"在 A 店和 B 店得到同一句答案。
     现在 = 平台通用基线 + 本店专属，同问法本店覆盖平台。
     要证明的：
       1. 两个店问同一句话，答案不同（且缓存不串店）
       2. 本店专属条目覆盖同问法的平台条目，平台那条不再进入候选
       3. 归属写入权：商家只能写本店、平台通用只读、规则域不挂店、买家不能写
       4. 快捷问题与阈值调试器都按店铺算（点下去必须命中）

  B. 订单商品明细与运单号
     以前订单表只有金额和时间戳，判"虚假发货"（规则 2.4 扣 4 分）时系统里
     连运单号都没有。现在要证明：
       5. 订单选择器、纠纷详情、仲裁台都能看到"买的什么、几件、单价、哪个快递、单号"
       6. 发货后无揽收记录 → 判虚假发货，扣 4 分（而不是 0.5 分的超时）
       7. 商品小计与实付不一致能被标出来（退款金额不能直接按实付全退）

用法（项目根目录）：
    python tests/v8_e2e.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.getenv("V8_TEST_PORT", "8018"))
BASE = f"http://127.0.0.1:{PORT}"
TMP_DB = ROOT / "data" / "_v8_e2e.db"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    return ok


def wait_up(timeout: int = 120) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _clean_redis() -> None:
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


def login(username: str) -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=40)
    for pw in ("123456", "123456", "Ops@12345", "Admin@12345"):
        r = c.post("/api/auth/login", json={"username": username, "password": pw})
        if r.status_code == 200:
            return c
    raise SystemExit(f"登录失败 {username}")


def ask(c: httpx.Client, shop_id: int, text: str) -> tuple[str, dict]:
    s = c.post("/api/cs/sessions", json={"shop_id": shop_id, "scope": "shop"}).json()
    r = c.post(f"/api/cs/sessions/{s['id']}/messages", json={"text": text}).json()
    return s["id"], r


def run() -> None:
    buyer = login("lichen")
    mer = login("fengxia")       # 霞光家居旗舰店 -> shop 1
    mer2 = login("xumeng")       # 梦境数码专营店 -> shop 2
    ops = login("ops")

    # ---------------------------------------------------------------- A1
    print("\n[1] 板块口径按店铺算")
    b1 = mer.get("/api/cs/board", params={"scope": "shop"}).json()
    check("商家板块锁定本店", b1["shop_id"] == 1 and b1["shop_name"] == "霞光家居旗舰店",
          f"{b1.get('shop_id')} {b1.get('shop_name')}")
    check("本店有专属话术", b1["stats"]["own"] >= 5, str(b1["stats"]["own"]))
    check("平台通用基线仍在", b1["stats"]["global"] >= 70, str(b1["stats"]["global"]))

    bb1 = buyer.get("/api/cs/board", params={"scope": "shop", "shop_id": 1}).json()
    bb2 = buyer.get("/api/cs/board", params={"scope": "shop", "shop_id": 2}).json()
    check("买家选 1 号店看到本店专属", bb1["stats"]["own"] == b1["stats"]["own"],
          f"{bb1['stats']['own']} vs {b1['stats']['own']}")
    check("买家选 2 号店没有本店专属", bb2["stats"]["own"] == 0, str(bb2["stats"]["own"]))
    check("两家店的快捷问题不同",
          {c["q"] for c in bb1["chips"]} != {c["q"] for c in bb2["chips"]})
    own_chips = [c["q"] for c in bb1["chips"] if c.get("own")]
    check("快捷问题里出现本店专属条目", bool(own_chips), "、".join(own_chips[:2]))

    # ---------------------------------------------------------------- A2
    print("\n[2] 同一句话，两家店两个答案（缓存不串店）")
    _, r1 = ask(buyer, 1, "什么时候发货")
    _, r2 = ask(buyer, 2, "什么时候发货")
    check("1 号店答本店口径", "大件" in r1["reply"], r1["reply"][:36])
    check("2 号店答平台口径", "节假日顺延" in r2["reply"] and "大件" not in r2["reply"],
          r2["reply"][:36])
    check("两家店答案确实不同", r1["reply"] != r2["reply"])
    tr = [x for x in r1["trace"]["layers"] if x["layer"] == "faq_bm25"]
    check("检索带着店铺视野", bool(tr) and tr[0].get("shop_id") == 1, str(tr[:1])[:90])
    # 第二次问同一句：命中缓存也必须是本店答案
    _, again = ask(buyer, 1, "什么时候发货")
    check("重复提问仍答本店口径（缓存 key 带店铺）", "大件" in again["reply"],
          f"{again['source']} / {again['reply'][:24]}")

    # ---------------------------------------------------------------- A3
    print("\n[3] 归属写入权")
    new = mer.post("/api/cs/faq", json={
        "question": "能开发票吗", "answer": "本店支持开具电子普票，下单后 3 个工作日发到邮箱。",
        "category": "发票", "keywords": "发票,开票", "priority": 20}).json()
    check("商家新建条目自动落到本店", new.get("shop_id") == 1, str(new.get("shop_id")))
    check("商家新建条目先待审", new.get("status") == "pending", str(new.get("status")))

    # 审核通过后生效（审核流：商家写的口径要平台过审才进机器人）
    ops.post(f"/api/cs/faq/{new['id']}/review", json={"approve": True})
    _, ask_inv0 = ask(buyer, 1, "能开发票吗")
    check("审核通过后生效", "电子普票" in ask_inv0["reply"], ask_inv0["reply"][:30])

    patched = mer.patch(f"/api/cs/faq/{new['id']}", json={"answer": "本店支持电子普票，当天开、次日达。"})
    check("商家能改本店条目", patched.status_code == 200, f"HTTP {patched.status_code}")
    _, ask_inv = ask(buyer, 1, "能开发票吗")
    check("改完立刻生效（索引已重建）", "当天开" in ask_inv["reply"], ask_inv["reply"][:30])

    glob = ops.get("/api/cs/faq", params={"audience": "buyer", "scope": "global",
                                          "limit": 300}).json()["items"]
    gid = glob[0]["id"]
    r = mer.patch(f"/api/cs/faq/{gid}", json={"answer": "改成我的说法"})
    check("商家改平台通用条目 → 403", r.status_code == 403, f"HTTP {r.status_code}")
    r = mer.delete(f"/api/cs/faq/{gid}")
    check("商家下架平台通用条目 → 403", r.status_code == 403, f"HTTP {r.status_code}")
    r = buyer.post("/api/cs/faq", json={"question": "买家能写知识吗", "answer": "不该能"})
    check("买家写知识库 → 403", r.status_code == 403, f"HTTP {r.status_code}")

    r = ops.post("/api/cs/faq", json={"question": "平台口径测试条目", "answer": "仅用于验收",
                                      "audience": "merchant", "shop_id": 1})
    check("规则域不挂店铺（shop_id 强制为空）", r.json().get("shop_id") is None,
          str(r.json().get("shop_id")))

    lst = mer.get("/api/cs/faq", params={"limit": 300}).json()
    own_rows = [x for x in lst["items"] if x["own"]]
    glob_rows = [x for x in lst["items"] if not x["own"]]
    check("商家列表同时列出本店与平台条目", bool(own_rows) and bool(glob_rows),
          f"own={len(own_rows)} global={len(glob_rows)}")
    check("平台通用条目对商家只读", all(not x["editable"] for x in glob_rows))
    check("本店条目对商家可编辑", all(x["editable"] for x in own_rows))
    check("列表标出条目归属店铺名", all(x["shop_name"] == "霞光家居旗舰店" for x in own_rows),
          own_rows[0]["shop_name"] if own_rows else "")

    g_all = ops.get("/api/cs/faq", params={"scope": "global", "limit": 300}).json()["items"]
    check("运营按 scope=global 只看到平台通用",
          all(x["shop_id"] is None for x in g_all) and len(g_all) >= 70, str(len(g_all)))
    own_all = ops.get("/api/cs/faq", params={"scope": "own", "shop_id": 1,
                                             "limit": 300}).json()["items"]
    check("运营按 scope=own 只看 1 号店", all(x["shop_id"] == 1 for x in own_all),
          str(len(own_all)))

    # ---------------------------------------------------------------- A4
    print("\n[4] 阈值调试器按店铺打分")
    p1 = ops.post("/api/cs/faq/probe", json={"query": "定制窗帘能退吗", "shop_id": 1}).json()
    p2 = ops.post("/api/cs/faq/probe", json={"query": "定制窗帘能退吗", "shop_id": 2}).json()
    check("1 号店命本店条目", p1["hit"] and p1["candidates"][0]["shop_id"] == 1,
          str(p1["candidates"][0]["sim"]) if p1["candidates"] else "无候选")
    top2 = [c for c in p2["candidates"] if c["question"] == "定制窗帘能退吗"]
    check("2 号店看不到别家店的专属条目", not top2, f"{len(top2)} 条")

    # ---------------------------------------------------------------- B5
    print("\n[5] 订单商品明细与运单号")
    orders = ops.get("/api/gov/orders", params={"shop_id": 1}).json()["items"]
    check("订单选择器带商品名", bool(orders) and all(o["item_title"] for o in orders),
          orders[0]["item_title"] if orders else "")
    # 订单选择器返回该店最近订单（含待付款/待发货），只有已发货类才有运单；
    # 旧断言「全部订单都有运单」只在"该店恰好只有 2 单且都已发货"时成立，不具一般性。
    _ship = ("已发货", "已完成", "售后中")
    _shipped = [o for o in orders if o["status"] in _ship]
    check("订单选择器带运单号", bool(_shipped) and all(o["tracking_no"] for o in _shipped),
          f"{len(_shipped)}/{len(orders)} 已发货单带运单")

    d = ops.post("/api/gov/disputes", json={
        "shop_id": 1, "dtype": "quality", "title": "【验收】商品明细核对",
        "claim": "收到的边几有裂痕", "claim_amount": 128.0,
        "order_no": orders[0]["order_no"],
        "evidence": [{"k": "商品实拍", "v": "边角开裂"}]}).json()
    det = ops.get(f"/api/gov/disputes/{d['id']}").json()
    od = det["order"]
    check("纠纷详情带商品明细", bool(od["item"]["title"]), od["item"]["title"])
    check("商品数量与单价可读", od["item"]["qty"] >= 1 and od["item"]["unit_price"] > 0,
          f"{od['item']['qty']} × {od['item']['unit_price']}")
    check("纠纷详情带承运商与运单号",
          bool(od["logistics"]["carrier"]) and bool(od["logistics"]["tracking_no"]),
          f"{od['logistics']['carrier']} {od['logistics']['tracking_no']}")
    check("已签收订单不算虚假发货", od["fake_ship"] is False, str(od["fake_ship"]))
    # 轨迹节点：订单表只有揽收一个时间点，这里要能看到「已下单→揽收→运输→派送→签收」
    # 的完整链路（由物流轨迹层按运单号补全），否则「查运单号」就只是读一个静态字段。
    tr = od["logistics"].get("trace") or []
    statuses = [n["status"] for n in tr]
    check("物流轨迹含完整节点链",
          "created" in statuses and "accepted" in statuses and "signed" in statuses,
          "→".join(statuses))
    check("轨迹揽收时间与订单揽收一致",
          any(n["status"] == "accepted" and n["time"][:16] == (od["logistics"]["picked_at"] or "")[:16]
              for n in tr),
          f"{od['logistics']['picked_at']} vs {[n['time'][:16] for n in tr if n['status']=='accepted']}")

    # ---------------------------------------------------------------- B6
    print("\n[6] 虚假发货判定（规则 2.4：扣 4 分）")
    # 直接往验收库里造一张"点了发货但没有揽收"的订单：这是平台侧最常见的
    # 虚假发货形态，靠页面操作造不出来，只能在数据层构造。
    sys.path.insert(0, str(ROOT))
    os.environ["DB_BACKEND"] = "sqlite"
    os.environ["SQLITE_PATH"] = str(TMP_DB)
    import common  # noqa: E402
    import datetime as _dt
    back = lambda h: (_dt.datetime.now() - _dt.timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")
    fake_no = "SOTESTFAKE001"
    common.execute("DELETE FROM t_order WHERE order_no=%s", (fake_no,))
    common.insert(
        """INSERT INTO t_order (order_no, shop_name, category, amount, status, paid_at,
               shipped_at, received_at, item_title, sku, qty, unit_price, carrier,
               tracking_no, picked_at, remarks)
           VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,NULL,%s)""",
        (fake_no, "霞光家居旗舰店", "家居", 99.0, "已发货", back(96), back(60),
         "实木置物架", "胡桃色 / 三层", 1, 99.0, "圆通速递", "YT0000000001", "验收用假发货订单"))

    d2 = ops.post("/api/gov/disputes", json={
        "shop_id": 1, "dtype": "shipping", "title": "【验收】点了发货但物流不动",
        "claim": "下单四天，物流一直没有揽收记录", "claim_amount": 99.0,
        "order_no": fake_no, "evidence": [{"k": "订单截图", "v": fake_no}]}).json()
    det2 = ops.get(f"/api/gov/disputes/{d2['id']}").json()
    check("识别为虚假发货", det2["order"]["fake_ship"] is True)
    check("揽收等待时长可读", (det2["order"]["logistics"]["pick_wait_hours"] or 0) > 24,
          str(det2["order"]["logistics"]["pick_wait_hours"]))
    check("虚假发货扣 4 分（不是超时的 0.5 分）", det2["analysis"]["points"] == 4.0,
          str(det2["analysis"]["points"]))
    steps = "".join(det2["analysis"]["steps"])
    check("说理里写明虚假发货与条款依据", "虚假发货" in steps and "2.4" in steps,
          steps[:60])

    # ---------------------------------------------------------------- B7
    print("\n[7] 商品小计与实付不一致")
    mis_no = "SOTESTMIS001"
    common.execute("DELETE FROM t_order WHERE order_no=%s", (mis_no,))
    common.insert(
        """INSERT INTO t_order (order_no, shop_name, category, amount, status, paid_at,
               shipped_at, received_at, item_title, sku, qty, unit_price, carrier,
               tracking_no, picked_at, remarks)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (mis_no, "霞光家居旗舰店", "家居", 180.0, "已完成", back(200), back(190), back(150),
         "棉麻抱枕", "45×45cm / 两只装", 2, 100.0, "顺丰速运", "SF0000000002",
         back(186), "验收用金额不一致订单"))
    d3 = ops.post("/api/gov/disputes", json={
        "shop_id": 1, "dtype": "other", "title": "【验收】部分退款核算",
        "claim": "两只抱枕退一只", "claim_amount": 100.0, "order_no": mis_no,
        "evidence": [{"k": "聊天记录", "v": "协商退一只"}]}).json()
    det3 = ops.get(f"/api/gov/disputes/{d3['id']}").json()
    check("标出商品小计与实付不一致", det3["order"]["amount_mismatch"] is True,
          f"小计 {det3['order']['item']['total']} / 实付 {det3['order']['amount']}")

    # ---------------------------------------------------------------- B8
    print("\n[8] 多商品订单与部分退款（t_order_item）")
    # 直接造一张两行商品的单：主件 + 配件，配件已退一只。
    # 单行字段表达不了「退哪一行」，这正是子表要解决的问题。
    multi_no = "SOTESTMULTI01"
    common.execute("DELETE FROM t_order_item WHERE order_no=%s", (multi_no,))
    common.execute("DELETE FROM t_order WHERE order_no=%s", (multi_no,))
    common.insert(
        """INSERT INTO t_order (order_no, shop_name, category, amount, status, paid_at,
               shipped_at, received_at, item_title, sku, qty, unit_price, carrier,
               tracking_no, picked_at, remarks)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (multi_no, "霞光家居旗舰店", "家居", 300.0, "售后中", back(200), back(190), back(150),
         "实木边几", "胡桃色 / 40×40cm", 1, 255.0, "顺丰速运", "SF0000000003",
         back(186), "验收用多商品订单"))
    common.insert(
        "INSERT INTO t_order_item (order_no, line_no, title, sku, qty, unit_price, "
        "refund_qty, refund_amount) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (multi_no, 1, "实木边几", "胡桃色 / 40×40cm", 1, 255.0, 0, 0))
    common.insert(
        "INSERT INTO t_order_item (order_no, line_no, title, sku, qty, unit_price, "
        "refund_qty, refund_amount) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (multi_no, 2, "防滑脚垫", "深棕 / 40mm", 4, 11.25, 1, 11.25))

    d4 = ops.post("/api/gov/disputes", json={
        "shop_id": 1, "dtype": "aftersale", "title": "【验收】只退其中一件",
        "claim": "脚垫只退一只", "claim_amount": 11.25, "order_no": multi_no,
        "evidence": [{"k": "聊天记录", "v": "协商退一只脚垫"}]}).json()
    det4 = ops.get(f"/api/gov/disputes/{d4['id']}").json()
    od4 = det4["order"]
    check("订单带两行商品", len(od4["items"]) == 2, str(len(od4["items"])))
    check("商品小计=各行合计且=实付（不算金额不一致）",
          abs(od4["subtotal"] - 300.0) < 0.01 and od4["amount_mismatch"] is False,
          f"subtotal={od4['subtotal']}")
    line2 = od4["items"][1]
    check("子表行带部分退款数量", line2["refund_qty"] == 1, str(line2["refund_qty"]))
    check("整单已退金额可读", abs(od4["refunded_amount"] - 11.25) < 0.01,
          str(od4["refunded_amount"]))
    # 带单核查：多商品要逐行列，且不把「配件已退」漏掉
    gs = ops.post("/api/cs/sessions", json={"scope": "gov"}).json()
    brief = ops.post(f"/api/cs/sessions/{gs['id']}/case",
                     json={"dispute_id": d4["id"]}).json()
    check("带单核查列出多商品", "共 5 件" in brief["reply"] and "防滑脚垫" in brief["reply"],
          brief["reply"][brief["reply"].find("【订单】"):brief["reply"].find("【订单】") + 80])
    check("带单核查标出已退件", "已退 1 件" in brief["reply"])

    for c in (buyer, mer, mer2, ops):
        c.close()


def main() -> int:
    src = ROOT / "data" / "rules.db"
    if not src.exists():
        print("缺少 data/rules.db，先跑一次 python run.py 生成基础库")
        return 2
    shutil.copy2(src, TMP_DB)
    _clean_redis()
    (ROOT / "logs").mkdir(exist_ok=True)
    env = {**os.environ, "LLM": "off", "DB_BACKEND": "sqlite", "SQLITE_PATH": str(TMP_DB)}
    log = open(ROOT / "logs" / "v8_e2e.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_up():
            print("服务未能在 120 秒内启动，见 logs/v8_e2e.log")
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
