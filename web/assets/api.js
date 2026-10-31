/* 全站共用：请求封装、登录态、提示、主题。
   原则：所有插入 DOM 的动态文本都必须过 esc()。用户输入的问题会原样回显，
   不转义就是一个现成的 XSS。 */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

/* ---------------- 基础工具 ---------------- */

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtTime(v) {
  if (!v) return "—";
  const d = new Date(String(v).replace(" ", "T"));
  if (isNaN(d)) return String(v);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")}`;
}

function fmtFull(v) {
  if (!v) return "—";
  return String(v).replace("T", " ").slice(0, 19);
}

/* 罚单状态徽章。三种状态必须分得清：生效中 / 已到期 / 已撤销。
   以前非 active 一律渲染成「已撤销」，罚单到期自动回收后，商家会看到
   一条自己从没申诉过的「已撤销」记录——这是把「到期」说成了「被推翻」。 */
function penBadge(status) {
  const map = {
    active: ["L3", "生效中"],
    expired: ["neutral", "已到期"],
    revoked: ["neutral", "已撤销"],
  };
  const [cls, text] = map[status] || ["neutral", status || "—"];
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}

/* ---------------- 提示 ---------------- */

function toast(msg, type = "info", ms = 3200) {
  let box = $("#toasts");
  if (!box) {
    box = document.createElement("div");
    box.id = "toasts";
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  const icon = type === "ok" ? "✓" : type === "err" ? "!" : "i";
  el.innerHTML = `<span style="font-weight:700;color:var(--${type === "ok" ? "ok" : type === "err" ? "danger" : "primary"})">${icon}</span><span>${esc(msg)}</span>`;
  box.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transform = "translateX(12px)"; el.style.transition = ".2s"; }, ms - 220);
  setTimeout(() => el.remove(), ms);
}

function confirmBox(title, message, okText = "确认", danger = false) {
  return new Promise(resolve => {
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML = `
      <div class="modal">
        <div class="modal-head">${esc(title)}</div>
        <div class="modal-body">${esc(message)}</div>
        <div class="modal-foot">
          <button class="btn" data-no>取消</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-yes>${esc(okText)}</button>
        </div>
      </div>`;
    document.body.appendChild(mask);
    const close = v => { mask.remove(); resolve(v); };
    mask.addEventListener("click", e => {
      if (e.target === mask || e.target.hasAttribute("data-no")) close(false);
      if (e.target.hasAttribute("data-yes")) close(true);
    });
    document.addEventListener("keydown", function k(e) {
      if (e.key === "Escape") { document.removeEventListener("keydown", k); close(false); }
    });
  });
}

/* ---------------- 请求 ---------------- */

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

/* noAuthRedirect：401 时不跳登录页，交给调用方自己处理。
   登录页就靠它探测登录态——否则 /api/auth/me 返回 401 会触发跳转到 /login，
   页面重载后又探测一次，形成无限刷新（这个人真踩过）。 */
let _authRedirecting = false;

async function api(path, { method = "GET", body = null, quiet = false,
                           noAuthRedirect = false } = {}) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (body !== null) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opt);
  } catch (e) {
    if (!quiet) toast("网络异常，请检查服务是否在运行", "err");
    throw new ApiError(0, "网络异常");
  }

  if (res.status === 401) {
    // 三重保险：调用方明确说"别跳"、已经在登录页、或者已经跳过一次了，都不再跳。
    // 少任何一条都可能变成刷新死循环。
    if (!noAuthRedirect && !_authRedirecting && location.pathname !== "/login") {
      _authRedirecting = true;
      const next = encodeURIComponent(location.pathname + location.search);
      location.replace(`/login?next=${next}`);
    }
    throw new ApiError(401, "未登录");
  }

  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    try { data = await res.json(); } catch (e) { data = null; }
  }

  if (!res.ok) {
    // FastAPI 的 HTTPException 是 {detail}，校验错误是 {detail:[{msg}]}
    let msg = "请求失败";
    if (data && data.detail) {
      msg = typeof data.detail === "string" ? data.detail
        : Array.isArray(data.detail) ? data.detail.map(d => d.msg).join("；")
          : JSON.stringify(data.detail);
    } else {
      msg = `服务异常 (${res.status})`;
    }
    if (!quiet) toast(msg, "err");
    throw new ApiError(res.status, msg);
  }
  return data;
}

/* ---------------- 登录态 ---------------- */

let _me = null;

async function getMe(force = false) {
  if (_me && !force) return _me;
  _me = await api("/api/auth/me", { quiet: true });
  return _me;
}

async function logout() {
  try { await api("/api/auth/logout", { method: "POST", quiet: true }); } catch (e) {}
  location.href = "/login";
}

/* ---------------- 主题 ---------------- */

function applyTheme(t) {
  const real = t === "auto"
    ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : t;
  document.documentElement.setAttribute("data-theme", real);
  localStorage.setItem("rr_theme", t);
}

function initTheme() {
  const saved = localStorage.getItem("rr_theme") || "auto";
  applyTheme(saved);
  if (saved === "auto") {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme("auto"));
  }
  return saved;
}

function toggleTheme() {
  const cur = localStorage.getItem("rr_theme") || "auto";
  const next = cur === "auto" ? "light" : cur === "light" ? "dark" : "auto";
  applyTheme(next);
  toast(`主题：${{ auto: "跟随系统", light: "浅色", dark: "深色" }[next]}`, "info", 1600);
  return next;
}

/* ---------------- 侧边栏 ---------------- */

/* 四个端各自该看到什么，一张表说清：
   买家只有 咨询/订单/纠纷/设置；商家多了 工作台/经营看板/客服台/知识库；
   运营多了 平台监管；管理员只有 系统面。不在这张表里的页面，导航直接不渲染
   （服务端路由另有拦截，两边都拦才算数）。 */
const ALL_ROLES = ["buyer", "merchant", "ops", "admin"];
const NAV = [
  // 「工作台」已并入各端的咨询窗口（/cs）与系统管理（/admin），不再单独存在
  { href: "/app", label: "工作台", icon: "◈", roles: [] },
  { href: "/dashboard", label: "经营看板", icon: "▤", roles: ["merchant"] },
  { href: "/rules", label: "知识库", icon: "▤", roles: ["merchant", "ops"] },
  // 咨询窗口按角色各进各的板块：买家店铺咨询 / 商家规则咨询 / 运营治理咨询。
  // 管理员没有咨询功能——不参与任何业务，不能和运营共用同一个治理界面。
  { href: "/cs", label: "咨询窗口", icon: "◔", roles: ["buyer", "merchant", "ops"],
    labelByRole: { buyer: "店铺咨询", merchant: "规则咨询", ops: "治理咨询" } },
  { href: "/orders", label: "我的订单", icon: "▦", roles: ["buyer"] },
  { href: "/service", label: "客服工作台", icon: "☏", roles: ["merchant", "ops"] },
  { href: "/dispute", label: "纠纷中心", icon: "⚖", roles: ["buyer", "merchant", "ops"] },
  { href: "/platform", label: "平台监管", icon: "⛨", roles: ["ops"] },
  { href: "/settings", label: "设置", icon: "⚙", roles: ALL_ROLES },
  { href: "/admin", label: "系统管理", icon: "◧", roles: ["admin"] },
];

async function initShell() {
  initTheme();
  const { user } = await getMe();

  $$("[data-nav]").forEach(el => {
    const item = NAV.find(n => n.href === el.dataset.nav);
    if (!item) return;
    // 该端没有这个功能 → 导航里直接不渲染（服务端路由另有拦截）
    if (!item.roles.includes(user.role)) { el.remove(); return; }
    const lb = item.labelByRole && item.labelByRole[user.role];
    if (lb) {
      const ico = el.querySelector(".ico");
      el.textContent = "";
      if (ico) el.appendChild(ico);
      el.appendChild(document.createTextNode(lb));
    }
  });

  const chip = $("#userChip");
  if (chip) {
    const initial = (user.display_name || user.username).slice(0, 1).toUpperCase();
    chip.innerHTML = `
      <div class="avatar" style="background:${esc(user.avatar_color || "#4f7cff")}">${esc(initial)}</div>
      <div class="user-meta">
        <div class="user-name">${esc(user.display_name || user.username)}</div>
        <div class="user-role">${esc(user.role_label)}${user.shop_name ? " · " + esc(user.shop_name) : ""}</div>
      </div>
      <span style="opacity:.5;font-size:11px">⌃</span>`;
    chip.addEventListener("click", async () => {
      const ok = await confirmBox("退出登录", "确定要退出当前账号吗？", "退出", true);
      if (ok) logout();
    });
  }
  return user;
}

function riskBadge(lv) {
  const map = { L1: "可直接回答", L2: "需附条款", L3: "强制转人工" };
  if (!lv) return "";
  return `<span class="badge ${esc(lv)}">${esc(lv)} ${esc(map[lv] || "")}</span>`;
}

/* 一条消息是谁发的。三方同处一个会话（买家 + 商家客服 + 平台运营）时，
   只靠 role=buyer/agent/bot 分不清人工客服到底是商家还是运营，
   所以优先用服务端记录的 sender_role。 */
function senderLabel(m) {
  const r = (m && m.sender_role) || "";
  const map = { buyer: "买家", merchant: "商家客服", ops: "平台运营",
                admin: "系统管理员", bot: "智能客服" };
  if (map[r]) return map[r];
  if (!m) return "";
  if (m.role === "buyer") return "买家";
  if (m.role === "agent") return "人工客服";
  if (m.role === "bot") return "智能客服";
  return "";
}

/* 身份徽标颜色：买家/商家/运营三色区分 */
function senderTag(m) {
  const r = (m && m.sender_role) || (m && m.role) || "";
  const cls = r === "buyer" ? "buyer" : (r === "merchant") ? "merchant"
            : (r === "ops" || r === "admin") ? "ops" : "bot";
  return `<span class="sender-tag ${cls}">${esc(senderLabel(m))}</span>`;
}

function genLabel(gen, llm) {
  const g = gen || "template";
  if (g.startsWith("llm:")) return `大模型 ${g.slice(4)}`;
  if (llm && llm.fallback === "citation_check_failed") return "模板（引用校验拦截）";
  if (llm && llm.fallback === "call_failed") return "模板（模型降级）";
  return "模板抽取";
}

/* 头部动作按钮按服务端给的动作画（v9 板块收口）。
   id 只在 kind=escalate 时挂 phEscalate，其他板块根本不会生成这个节点——
   「页面上有没有这个按钮」由数据决定，不由某个 if 分支的 else 决定。
   纯数据驱动，输入 acts 数组、输出 HTML 片段，方便单元回归。 */
function paintActions(acts) {
  const st = "color:#cbd5e1;border-color:rgba(255,255,255,.18)";
  let html = "";
  (acts || []).forEach(a => {
    if (a.kind === "escalate") {
      html += `<button id="phEscalate" class="btn btn-sm btn-ghost"
           style="color:#fca5a5;border-color:rgba(252,165,165,.3)"
           title="${esc(a.title || "")}">${esc(a.label)}</button>`;
    } else {
      html += `<a href="${esc(a.href || "#")}" class="btn btn-sm btn-ghost" style="${st}"
           title="${esc(a.title || "")}">${esc(a.label)}</a>`;
    }
  });
  return html;
}

