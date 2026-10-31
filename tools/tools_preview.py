"""临时脚本：用假数据把 service.html / cs.html 渲染成截图，检查布局。

只做视觉校验，不参与运行。用完即删（连同 web/assets/_pv 目录）。
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PV = WEB / "assets" / "_pv"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"

MOCK = r"""
/* ---------------- 预览用假数据 ---------------- */
const ME = { id: 3, username: "fengxia", display_name: "冯霞", role: "merchant",
  role_label: "商家", shop_name: "霞光家居旗舰店", shop_id: 1, avatar_color: "#0ea5a5" };
const SS = [
  { id: "s1", buyer_name: "橙子不加冰", channel: "淘宝", status: "bot", handoff: 0,
    last_msg: "亲，我们下单后 48 小时内安排出库…", msg_count: 2, ticket_id: null,
    ticket_status: null, updated_at: "2026-10-30 00:02:11" },
  { id: "s2", buyer_name: "小满", channel: "拼多多", status: "agent", handoff: 1,
    last_msg: "好的我这就帮您登记退货", msg_count: 6, ticket_id: 12,
    ticket_status: "processing", updated_at: "2026-10-30 00:01:40" },
  { id: "s3", buyer_name: "阿哲", channel: "抖音", status: "bot", handoff: 1,
    last_msg: "抱歉，这个问题我暂时没有把握准确回答…", msg_count: 2, ticket_id: 13,
    ticket_status: "pending", updated_at: "2026-10-29 23:58:02" },
  { id: "s4", buyer_name: "林小满", channel: "淘宝", status: "bot", handoff: 0,
    last_msg: "亲，可以领 10 元无门槛券…", msg_count: 2, ticket_id: null,
    ticket_status: null, updated_at: "2026-10-29 23:44:19" },
  { id: "s5", buyer_name: "匿名买家", channel: "拼多多", status: "closed", handoff: 1,
    last_msg: "人工客服已结束本次会话", msg_count: 8, ticket_id: 9,
    ticket_status: "done", updated_at: "2026-10-29 22:31:00" },
];
const MS = [
  { id: 1, role: "buyer", content: "在吗 我买的那个杯子什么时候能发货啊", source: "", score: 0, intent: "物流发货", latency_ms: 0, created_at: "" },
  { id: 2, role: "bot", content: "亲，我们下单后 48 小时内安排出库，节假日顺延。发货后系统会自动推送物流单号，您可以在「我的订单」里直接查看物流轨迹哦～", source: "faq", score: 0.9612, intent: "物流发货", latency_ms: 38, created_at: "" },
  { id: 3, role: "buyer", content: "能不能便宜点", source: "", score: 0, intent: "优惠价格", latency_ms: 0, created_at: "" },
  { id: 4, role: "bot", content: "亲，本店已经是活动价了，暂时没有额外折扣。不过您可以关注店铺领 10 元无门槛券，叠加平台满减会更划算，我发您链接。", source: "redis", score: 1.0, intent: "优惠价格", latency_ms: 3, created_at: "" },
  { id: 5, role: "buyer", content: "我买的沙发裂开了怎么办", source: "", score: 0, intent: "换货售后", latency_ms: 0, created_at: "" },
  { id: 6, role: "bot", content: "抱歉，这个问题我暂时没有把握准确回答，已经帮您转接人工客服，客服看到消息会第一时间回复您。您也可以先看看这条：「收到的商品有质量问题」", source: "template", score: 0.32, intent: "换货售后", latency_ms: 124, created_at: "" },
  { id: 7, role: "system", content: "冯霞 已接入会话", source: "system", score: 0, intent: "", latency_ms: 0, created_at: "" },
  { id: 8, role: "agent", content: "您好，我是售后专员冯霞。麻烦您拍几张裂开部位的照片发我，确认后我们优先补发或全额退款，不需要您寄回。", source: "human", score: 0, intent: "", latency_ms: 0, created_at: "" },
];
const FQ = [
  { id: 1, question: "什么时候发货", answer: "亲，我们下单后 48 小时内安排出库，节假日顺延。", category: "物流发货", hit_count: 128, source: "seed", keywords: "发货,出库", aliases: "多久发货|什么时候能发", priority: 0, status: "enabled", updated_at: "" },
  { id: 2, question: "几天能到", answer: "一般情况下发货后 3-5 天送达，偏远地区 7 天左右。", category: "物流发货", hit_count: 96, source: "seed", keywords: "到货", aliases: "什么时候能到|多久能到", priority: 0, status: "enabled", updated_at: "" },
  { id: 3, question: "可以开发票吗", answer: "支持电子普票和增值税专用发票，发货后 3 个工作日内开出。", category: "发票", hit_count: 71, source: "seed", keywords: "发票", aliases: "能开发票吗|要发票", priority: 0, status: "enabled", updated_at: "" },
  { id: 4, question: "可以便宜点吗", answer: "本店已是活动价，可以领 10 元无门槛券叠加平台满减。", category: "优惠价格", hit_count: 64, source: "seed", keywords: "优惠", aliases: "能便宜点吗|便宜点", priority: 0, status: "enabled", updated_at: "" },
  { id: 5, question: "沙发面料开裂属于质量问题吗", answer: "正常使用 30 天内非人为开裂属于质量问题，可免费换新。", category: "换货售后", hit_count: 3, source: "human", keywords: "", aliases: "", priority: 0, status: "enabled", updated_at: "" },
];
const TKS = [
  { id: 13, session_id: "s3", buyer_name: "阿哲", channel: "抖音", reason: "low_conf", reason_label: "知识库无答案", question: "你们家这个和图片颜色差好多，能换吗", bot_reply: "…", status: "pending", priority: 6, agent_reply: null, created_at: "2026-10-29 23:58:02", session_status: "bot" },
  { id: 14, session_id: "s5", buyer_name: "匿名买家", channel: "拼多多", reason: "complaint", reason_label: "投诉维权", question: "我买的这个用了三天就坏了，我要投诉你们", bot_reply: "…", status: "pending", priority: 9, agent_reply: null, created_at: "2026-10-29 23:50:11", session_status: "bot" },
  { id: 15, session_id: "s1", buyer_name: "橙子不加冰", channel: "淘宝", reason: "user_request", reason_label: "买家要求人工", question: "转人工", bot_reply: "…", status: "pending", priority: 7, agent_reply: null, created_at: "2026-10-29 23:41:00", session_status: "bot" },
];
const MT = { sessions: 128, handoff_sessions: 34, bot_replies: 176,
  by_source: { faq: 92, redis: 41, llm: 12, rule: 18, template: 13, human: 22 },
  auto_resolve_rate: 0.8239, handoff_rate: 0.2656, avg_latency_ms: 46,
  faq_hit_rate: 0.7557, tickets: { pending: 3, done: 26 }, ticket_pending: 3,
  faq: { faqs: 74, threshold: 0.88, version: 3 }, llm: {}, redis: {} };

const _api = async function (path, opt = {}) {
  await new Promise(r => setTimeout(r, 30));
  if (path.startsWith("/api/auth/me")) return { user: ME };
  if (path === "/api/cs/sessions" && (opt.method || "GET") === "POST") return { id: "s1" };
  if (path.startsWith("/api/cs/sessions?") || path === "/api/cs/sessions")
    return { items: SS };
  if (/^\/api\/cs\/sessions\/[^/]+$/.test(path))
    return { session: SS[0], messages: MS, can_escalate: true, dispute: null };
  if (path.startsWith("/api/cs/tickets")) return { items: TKS };
  if (path === "/api/cs/metrics") return MT;
  if (path.startsWith("/api/cs/faq/probe")) return { query: "我的退款什么时候能到账",
    normalized: "我的退款什么时候到账", features: ["退款", "我的", "的退", "退款", "款什", "什么", "么时", "时候", "候到", "到账"],
    threshold: 0.88, hit: false, version: 3,
    candidates: [
      { id: 9, question: "退款多久到账", answer: "…", sim: 0.718, cov: 0.66, cov_idf: 0.71, cov_cnt: 0.61, sat: 0.76, matched: ["退款", "到账"] },
      { id: 8, question: "怎么申请退款", answer: "…", sim: 0.412, cov: 0.33, cov_idf: 0.31, cov_cnt: 0.36, sat: 0.52, matched: ["退款"] },
      { id: 5, question: "可以只退款不退货吗", answer: "…", sim: 0.288, cov: 0.22, cov_idf: 0.2, cov_cnt: 0.24, sat: 0.41, matched: ["退款"] },
    ] };
  if (path.startsWith("/api/cs/faq")) return { items: FQ, categories: [{ name: "物流发货", n: 14 }, { name: "发票", n: 6 }], threshold: 0.88, version: 3 };
  return {};
};
api = _api;
initShell = async function () {
  initTheme();
  const chip = document.querySelector("#userChip");
  if (chip) chip.innerHTML = `<div class="avatar" style="background:${ME.avatar_color}">冯</div>
    <div class="user-meta"><div class="user-name">冯霞</div>
    <div class="user-role">商家 · 霞光家居旗舰店</div></div>`;
  return ME;
};
getMe = async () => ({ user: ME });
"""


def build(name: str) -> pathlib.Path:
    src = (WEB / f"{name}.html").read_text(encoding="utf-8")
    apijs = (WEB / "assets" / "api.js").read_text(encoding="utf-8")
    src = src.replace('<script src="/assets/api.js"></script>',
                      f"<script>{apijs}</script>\n<script>{MOCK}</script>")
    PV.mkdir(parents=True, exist_ok=True)
    out = PV / f"{name}.html"
    out.write_text(src, encoding="utf-8")
    return out


def shot(name: str, width: int, height: int, out: pathlib.Path, wait_ms: int = 2500):
    profile = PV / "profile"
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    f"--user-data-dir={profile}", "--no-first-run",
                    f"--window-size={width},{height}",
                    f"--virtual-time-budget={wait_ms}",
                    f"--screenshot={out}", f"{BASE}/assets/_pv/{name}.html"],
                   capture_output=True, timeout=90)


def main():
    env = {**os.environ, "DB_BACKEND": "sqlite"}
    log = open(ROOT / "logs" / "preview.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host",
                             "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(90):
            if subprocess.run(["curl", "-s", "--noproxy", "*", "-m", "2",
                               f"{BASE}/api/health"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        outs = []
        for name, w, h in [("service", 1560, 980), ("cs", 1100, 900)]:
            build(name)
            p = ROOT / f"logs/_shot_{name}.png"
            shot(name, w, h, p)
            outs.append(str(p))
            print("shot:", p, p.exists())
        print(json.dumps(outs))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
        shutil.rmtree(PV, ignore_errors=True)


if __name__ == "__main__":
    main()
