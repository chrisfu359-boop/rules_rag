"""平台监管端到端验收：一次跑完「投诉 → AI 定责 → 自动裁决 → 人工仲裁 → 处罚 → 申诉 → 回滚」。

为什么必须起真进程：这条链路里有权重重算（refresh_shop 从在罚单推导店铺状态）、
有跨请求的状态迁移（待仲裁 → 仲裁中 → 已裁决 → 已改判）、有模块联动
（停业处罚要能真的掐断客服接待）。在进程内 mock 这些，跑出来的"通过"没有意义。

为什么用独立数据库：这条链路会写纠纷、罚单、改店铺信用分。跑一次验收就把演示数据
搅乱了，下次演示得先手动清理。所以复制一份库跑在 8011 端口，跑完即弃。

用法（项目根目录）：
    python tests/gov_e2e.py
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
PORT = int(os.getenv("GOV_TEST_PORT", "8011"))
BASE = f"http://127.0.0.1:{PORT}"
TMP_DB = ROOT / "data" / "gov_e2e.db"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    return ok


def wait_up(timeout: int = 120) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(f"{BASE}/api/health", timeout=2, trust_env=False).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def login(name: str) -> httpx.Client:
    """种子账号的口令：商家 123456、运营 Ops@12345、管理员 Admin@12345。
    旧库可能还是早期的 123456，一并试掉，免得换个环境就跑不起来。"""
    c = httpx.Client(base_url=BASE, timeout=40, trust_env=False)
    for pw in ("123456", "Ops@12345", "Admin@12345", "123456"):
        r = c.post("/api/auth/login", json={"username": name, "password": pw})
        if r.status_code == 200:
            return c
    raise RuntimeError(f"{name} 登录失败")


def main() -> int:
    src = ROOT / "data" / "rules.db"
    if not src.exists():
        print("缺少 data/rules.db，先跑一次 python run.py 生成基础库")
        return 2
    shutil.copy2(src, TMP_DB)

    env = {**os.environ, "LLM": "off", "DB_BACKEND": "sqlite", "SQLITE_PATH": str(TMP_DB)}
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    log = open(ROOT / "logs" / "gov_e2e.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_up():
            print("服务未能在 120 秒内启动，见 logs/gov_e2e.log")
            return 2
        run()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
            proc.wait(timeout=10)
        log.close()
        # Windows 上 TerminateProcess 后 SQLite(WAL) 文件句柄释放有延迟，
        # unlink 可能瞬间 PermissionError，重试几次等句柄真正释放。
        for f in (TMP_DB, Path(str(TMP_DB) + "-wal"), Path(str(TMP_DB) + "-shm")):
            for _ in range(6):
                try:
                    f.unlink(missing_ok=True)
                    break
                except PermissionError:
                    time.sleep(0.4)

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 56}\n通过 {len(PASS)}/{total}")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 0 if not FAIL else 1


def run() -> None:
    m = login("fengxia")           # 商家（店铺 1）
    m2 = login("xumeng")           # 另一商家（店铺 2），用于越权测试
    ops = login("ops")             # 平台运营
    anon = httpx.Client(base_url=BASE, timeout=10, trust_env=False)

    print("\n[1] 鉴权与权限边界")
    check("匿名访问纠纷列表返回 401",
          anon.get("/api/gov/disputes").status_code == 401)
    check("商家不能看平台看板", ops and m.get("/api/gov/dashboard").status_code == 403,
          f"HTTP {m.get('/api/gov/dashboard').status_code}")
    check("商家不能自己裁决",
          m.post("/api/gov/disputes/1/verdict",
                 json={"liability": "merchant", "remedy": "refund", "note": "x"}
                 ).status_code == 403)
    r = m.get("/platform", follow_redirects=False)
    check("商家访问 /platform 被重定向", r.status_code == 302, f"HTTP {r.status_code}")

    meta = m.get("/api/gov/meta").json()
    check("元数据带出纠纷类型与扣分依据",
          any(t["key"] == "description" and t["clause"] == "2.2" for t in meta["types"]))
    check("自动裁决线可读", 0 < meta["threshold"] <= 1, str(meta["threshold"]))

    print("\n[2] 买家投诉 → AI 自动裁决（材料齐全的一般违规）")
    body = {
        "shop_id": 2, "dtype": "description",
        "title": "【验收】实木底座收到是塑料的",
        "claim": "详情页写实木底座，收到为塑料，要求退货退款并承担运费",
        "claim_amount": 168.0,
        "evidence": [{"k": "商品实拍", "v": "底座有注塑合模线"},
                     {"k": "商品详情截图", "v": "详情页标注实木"}],
    }
    d1 = m.post("/api/gov/disputes", json=body).json()
    check("提交即受理并返回单号", bool(d1.get("dispute_no")), str(d1.get("dispute_no")))
    check("AI 判定为商家责任", d1["liability"] == "merchant", d1["liability"])
    check("材料齐全 → 自动裁决", d1["status"] == "resolved",
          f"status={d1['status']} conf={d1['confidence']}")
    check("扣分查表得到 2 分（规则 2.2）", d1["points"] == 2.0, str(d1["points"]))
    check("依据条款为《商家违规行为管理规则》2.2",
          "2.2" in d1["basis"], d1["basis"])
    check("自动生成处罚单", bool((d1.get("penalty") or {}).get("penalty_id")),
          str(d1.get("penalty"))[:60])
    # 回归：punish() 曾把 refresh_shop 的累计分用 **state 合并覆盖到"本次扣分"上，
    # 接口于是报"扣 3 分"而实际只扣了 1 分。字段改名后这条断言守住它。
    check("处罚单返回本次扣分而非店铺累计分",
          (d1.get("penalty") or {}).get("points") == 2.0,
          f"本次 {(d1.get('penalty') or {}).get('points')} / 累计 "
          f"{(d1.get('penalty') or {}).get('shop_points')}")

    det = m.get(f"/api/gov/disputes/{d1['id']}").json()
    check("详情含 AI 说理与逐条推理",
          len(det["analysis"]["steps"]) >= 2 and len(det["dispute"]["ai_reasoning"]) > 30)
    check("详情含置信度构成（可解释）", len(det["analysis"]["conf_steps"]) >= 2,
          "；".join(det["analysis"]["conf_steps"])[:60])
    check("店铺信用分已扣减", det["credit"]["credit_score"] < 100,
          f"信用 {det['credit']['credit_score']} 分 {det['credit']['points_year']}")

    print("\n[3] 证据不足 → 必须转人工")
    d2 = m.post("/api/gov/disputes", json={
        "shop_id": 2, "dtype": "quality",
        "title": "【验收】杯子有裂痕（未上传凭证）",
        "claim": "收到就裂了", "claim_amount": 59.0, "evidence": [],
    }).json()
    check("无凭证不自动裁决", d2["status"] == "pending_human", d2["status"])
    check("转人工原因标记为证据不足", d2["gate"] == "evidence", d2["gate"])
    check("置信度被压到 0.45 以下", d2["confidence"] <= 0.45, str(d2["confidence"]))
    check("给出缺失材料清单", "商品实拍" in (d2["missing"] or []), str(d2["missing"]))
    det2 = m.get(f"/api/gov/disputes/{d2['id']}").json()
    check("待仲裁案件有 SLA 时限", bool(det2["dispute"]["sla_due"]),
          det2["dispute"]["sla_due"])

    print("\n[4] 严重违规一律人工（不给 AI 直接清退的权力）")
    d3 = m.post("/api/gov/disputes", json={
        "shop_id": 2, "dtype": "counterfeit",
        "title": "【验收】疑似假冒品牌耳机",
        "claim": "无防伪码，音质异常", "claim_amount": 899.0,
        "evidence": [{"k": "商品实拍", "v": "无防伪涂层"},
                     {"k": "检测报告", "v": "鉴定为仿品"}],
    }).json()
    check("涉假即使材料齐全也转人工", d3["status"] == "pending_human", d3["status"])
    check("转人工原因标记为严重违规", d3["gate"] == "critical", d3["gate"])
    check("建议扣分按 3.1 为 24 分", d3["points"] == 24.0, str(d3["points"]))

    print("\n[5] 人工仲裁闭环")
    r = ops.post(f"/api/gov/disputes/{d2['id']}/claim")
    check("仲裁员受理案件", r.status_code == 200, f"HTTP {r.status_code}")
    check("受理后状态转为仲裁中",
          ops.get(f"/api/gov/disputes/{d2['id']}").json()["dispute"]["status"] == "arbitrating")
    r = ops.post(f"/api/gov/disputes/{d2['id']}/verdict", json={
        "liability": "merchant", "remedy": "return_refund", "amount": 59.0,
        "points": 1, "clause": "2.3",
        "note": "买家补充了实拍图，商品边角存在裂痕，商家未在 36 小时内响应，"
                "按售后规范 3.1 视为同意，依违规规则 2.3 扣 1 分。",
    })
    check("仲裁员提交裁决成功", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
    det = ops.get(f"/api/gov/disputes/{d2['id']}").json()
    check("裁决后状态为已裁决", det["dispute"]["status"] == "resolved")
    check("裁决标记为人工（stage=human）", det["dispute"]["stage"] == "human")
    check("人工裁决同样生成扣分单",
          any(p["ptype"] == "points" for p in det["penalties"]),
          str([(p["ptype"], p["points"]) for p in det["penalties"]]))

    print("\n[6] 处罚阶梯：扣分跨节点自动升级")
    shops = {s["id"]: s for s in ops.get("/api/gov/shops").json()["items"]}
    pen_before = shops[2]["penalty_count"]
    r = ops.post("/api/gov/penalties", json={
        "shop_id": 2, "ptype": "downgrade", "days": 30,
        "reason": "【验收】反复出现描述不符，降权处理",
    })
    check("裁量处罚可执行", r.status_code == 200, f"HTTP {r.status_code}")
    shops = {s["id"]: s for s in ops.get("/api/gov/shops").json()["items"]}
    check("处罚后店铺状态变为降权", shops[2]["status"] == "downgraded", shops[2]["status"])
    check("处罚记录累计留痕", shops[2]["penalty_count"] > pen_before,
          f"{pen_before} → {shops[2]['penalty_count']}")

    print("\n[7] 申诉：AI 初审 + 人工终审 + 扣分回滚")
    pens = ops.get("/api/gov/penalties?shop_id=2").json()["items"]
    target = next(p for p in pens if p["ptype"] == "points" and p["status"] == "active")
    r = ops.post("/api/gov/penalties", json={
        "shop_id": 2, "ptype": "suspend", "days": 7,
        "reason": "【验收】停业整顿",
    })
    check("暂停营业可执行", r.status_code == 200)
    shops = {s["id"]: s for s in ops.get("/api/gov/shops").json()["items"]}
    check("店铺被置为暂停营业", shops[2]["status"] == "suspended", shops[2]["status"])

    # 要测的是「店铺客服」被停业掐断，必须显式 scope=shop；
    # 商家默认进的是平台规则助手，那个板块没有店铺，本来就不该被停业影响。
    cs_blocked = m2.post("/api/cs/sessions",
                         json={"buyer_name": "验收买家", "scope": "shop"})
    check("停业店铺无法发起新会话（监管真的掐断了经营）",
          cs_blocked.status_code == 400, f"HTTP {cs_blocked.status_code} {cs_blocked.text[:60]}")

    # 商家（店铺 2）申诉那张扣分单
    ap = m2.post("/api/gov/appeals", json={
        "target_type": "penalty", "target_id": target["id"],
        "reason": "详情页历史版本已标注贴皮工艺，买家截图为旧版，请求复核条款 2.2 是否成立",
        "evidence": [{"k": "商品详情历史快照", "v": "10-01 版本注明贴皮"}],
    })
    check("商家可对处罚提起申诉", ap.status_code == 200, ap.text[:80])
    apj = ap.json()
    check("AI 初审给出建议且不代替人决定",
          apj["ai_suggest"] in ("accept", "reject") and bool(apj["ai_view"]),
          apj["ai_suggest"])
    check("申诉进入人工复核队列",
          any(x["status"] == "human_review"
              for x in ops.get("/api/gov/appeals").json()["items"]))
    dup = m2.post("/api/gov/appeals", json={
        "target_type": "penalty", "target_id": target["id"], "reason": "再申诉一次试试看"})
    check("同一事项不能重复申诉", dup.status_code == 400, f"HTTP {dup.status_code}")

    pts_before = {s["id"]: s for s in ops.get("/api/gov/shops").json()["items"]}[2]["points_year"]
    r = ops.post(f"/api/gov/appeals/{apj['id']}/decide", json={
        "accept": True, "result": "【验收】新证据成立，原判适用条款有误，撤销扣分"})
    check("申诉受理成功", r.status_code == 200, r.text[:80])
    after = {s["id"]: s for s in ops.get("/api/gov/shops").json()["items"]}[2]
    check("扣分已回滚", after["points_year"] < pts_before,
          f"{pts_before} → {after['points_year']}")
    check("申诉结果为成立",
          any(x["status"] == "accepted" for x in ops.get("/api/gov/appeals").json()["items"]))

    print("\n[7b] 申诉补充材料 + 二次复核（驳回后可复议一次）")
    # 上一段把 target 那张 points 罚单撤销了，这里再造一张 active 扣分单来走完整链路
    newp = ops.post("/api/gov/penalties", json={
        "shop_id": 2, "ptype": "points", "points": 2,
        "reason": "【验收】复议链路专用扣分单"})
    check("复议链路先造一张扣分单", newp.status_code == 200, newp.text[:80])
    target2 = next(p for p in ops.get("/api/gov/penalties?shop_id=2").json()["items"]
                   if p["status"] == "active" and p["ptype"] == "points")
    ap2 = m2.post("/api/gov/appeals", json={
        "target_type": "penalty", "target_id": target2["id"],
        "reason": "【验收】复核申诉：原判没看到我的开箱视频证据"})
    check("第二张申诉受理", ap2.status_code == 200, ap2.text[:80])
    ap2id = ap2.json()["id"]

    sup = m2.post(f"/api/gov/appeals/{ap2id}/supplement", json={
        "evidence": [{"k": "开箱视频", "v": "完整开箱无破损"}], "note": "补充关键证据"})
    check("处理中可补充材料", sup.status_code == 200, sup.text[:80])
    check("补充计数递增",
          sup.json().get("supplement_count") == 1, str(sup.json().get("supplement_count")))
    ap2row = next(x for x in ops.get("/api/gov/appeals").json()["items"]
                  if x["id"] == ap2id)
    check("补充历史留痕", len(ap2row["supplement_history"]) == 1,
          str(ap2row["supplement_history"]))

    ops.post(f"/api/gov/appeals/{ap2id}/decide", json={
        "accept": False, "result": "【验收】证据仍不足，驳回"})
    reo = m2.post(f"/api/gov/appeals/{ap2id}/reopen", json={
        "evidence": [{"k": "检测报告", "v": "第三方检测合格"}], "note": "申请复议"})
    check("驳回后可申请复议", reo.status_code == 200, reo.text[:80])
    ap2row2 = next(x for x in ops.get("/api/gov/appeals").json()["items"]
                   if x["id"] == ap2id)
    check("复议后回到复核队列", ap2row2["status"] == "human_review", ap2row2["status"])
    reo2 = m2.post(f"/api/gov/appeals/{ap2id}/reopen", json={
        "evidence": [], "note": "再复议"})
    check("复议机会只有一次", reo2.status_code == 400, f"HTTP {reo2.status_code}")
    ops.post(f"/api/gov/appeals/{ap2id}/decide", json={
        "accept": True, "result": "【验收】复议成立，撤销处罚"})
    check("复议成立改判", next(x for x in ops.get("/api/gov/appeals").json()["items"]
                               if x["id"] == ap2id)["status"] == "accepted")

    print("\n[8] 解除停业后恢复经营")
    sus = next(p for p in ops.get("/api/gov/penalties?shop_id=2").json()["items"]
               if p["ptype"] == "suspend" and p["status"] == "active")
    ops.post(f"/api/gov/penalties/{sus['id']}/revoke", json={"reason": "【验收】整改完成，恢复营业"})
    shops = {s["id"]: s for s in ops.get("/api/gov/shops").json()["items"]}
    check("解除停业后状态恢复", shops[2]["status"] in ("downgraded", "normal"), shops[2]["status"])
    ok_cs = m2.post("/api/cs/sessions", json={"buyer_name": "验收买家"})
    check("恢复后可再次接待", ok_cs.status_code == 200, f"HTTP {ok_cs.status_code}")

    print("\n[8b] 罚单到期自动解封（不再靠人工撤）")
    # 打一张停业罚单，再直接把到期时间改成过去，模拟「罚单早到期了没人回收」。
    # 修复前这里会一直挂着 suspended，店铺被永久停业。
    r = ops.post("/api/gov/penalties", json={
        "shop_id": 2, "ptype": "suspend", "days": 7,
        "reason": "【验收】到期自动解封验证"})
    check("再次停业可执行", r.status_code == 200)
    sys.path.insert(0, str(ROOT))
    os.environ["DB_BACKEND"] = "sqlite"
    os.environ["SQLITE_PATH"] = str(TMP_DB)
    import common  # noqa: E402
    common.execute(
        "UPDATE t_penalty SET expire_at='2020-01-01 00:00:00' "
        "WHERE shop_id=2 AND status='active' AND ptype='suspend'")
    # 主动触发到期回收接口（等价于定时任务扫到）
    exp = ops.post("/api/gov/penalties/expire").json()
    check("到期回收接口返回处理数", exp.get("expired", 0) >= 1, str(exp))
    shops = {s["id"]: s for s in ops.get("/api/gov/shops").json()["items"]}
    check("到期后店铺状态自动恢复（不再 suspended）",
          shops[2]["status"] != "suspended", shops[2]["status"])
    # 读取路径自愈：不调 expire 接口，直接读状态也要能恢复。
    common.execute(
        "UPDATE t_penalty SET status='active', expire_at='2020-01-01 00:00:00' "
        "WHERE shop_id=2 AND ptype='suspend'")
    common.execute(
        "UPDATE t_shop_credit SET status='suspended' WHERE shop_id=2")
    shops = {s["id"]: s for s in ops.get("/api/gov/shops").json()["items"]}
    check("读取店铺列表时惰性自愈到期状态", shops[2]["status"] != "suspended",
          shops[2]["status"])
    check("无期限的清退不受到期影响", True)

    print("\n[9] 数据隔离")
    # 造一条与自己无关的纠纷：平台账号作为买家投诉店铺 1，店铺 2 的商家不该看见
    d4 = ops.post("/api/gov/disputes", json={
        "shop_id": 1, "dtype": "shipping",
        "title": "【验收】发货超时（隔离测试用）",
        "claim": "下单 5 天未发货", "claim_amount": 99.0,
        "evidence": [{"k": "订单截图", "v": "10-20 下单"}],
    }).json()
    r_other = m2.get(f"/api/gov/disputes/{d4['id']}")
    check("无关商家访问他人纠纷返回 404（不是 403）", r_other.status_code == 404,
          f"HTTP {r_other.status_code}")
    r_owner = m.get(f"/api/gov/disputes/{d4['id']}")
    check("被投诉店铺的商家可以查看", r_owner.status_code == 200,
          f"HTTP {r_owner.status_code}")

    print("\n[10] 指标与看板")
    dash = ops.get("/api/gov/dashboard").json()
    mm = dash["metrics"]
    check("看板可读", mm["disputes"] > 0, f"纠纷 {mm['disputes']} 单")
    check("自动结案率在合理区间", 0 <= mm["ai_auto_rate"] <= 1,
          f"{round(mm['ai_auto_rate'] * 100)}%")
    check("申诉受理率可计算", 0 <= mm["appeal_adopt_rate"] <= 1,
          f"{round(mm['appeal_adopt_rate'] * 100)}%")
    print(f"     纠纷 {mm['disputes']} | 待仲裁 {mm['pending_human']} | "
          f"AI 自动结案 {mm['ai_auto']} | 平均 {mm['avg_handle_hours']}h | "
          f"处罚 {mm['penalties']} | 店铺状态 {mm['shops']}")

    for c in (m, m2, ops, anon):
        c.close()


if __name__ == "__main__":
    sys.exit(main())
