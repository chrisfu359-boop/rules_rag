/* 前端组件回归：纯函数级验证（无需真实浏览器）。
 *
 * 覆盖 v9/v10/v11 里「前端渲染收口」的关键逻辑，这些以前全靠肉眼截图：
 *   - penBadge：罚单状态徽章（生效中/已到期/已撤销）必须分得清
 *   - paintActions：板块动作按钮由服务端数据驱动，escalate 只在买家板块出现
 *   - riskBadge：风险等级徽章（L1/L2/L3）
 *   - esc/fmtFull：转义与时间格式化（防 XSS / 时间展示口径）
 *
 * 运行（无需服务，纯 vm 沙箱加载 api.js）：
 *     node tests/ui_components_regression.js
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "web", "assets", "api.js"), "utf8");

function makeCtx() {
  const ctx = {
    location: { pathname: "/app", search: "", href: "", navigations: [] },
    document: {
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, remove() {} }),
      body: { appendChild() {} },
      documentElement: { setAttribute() {} },
      addEventListener() {},
    },
    localStorage: { getItem: () => null, setItem() {} },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    fetch: () => Promise.reject(new Error("no network in unit test")),
    console, setTimeout, clearTimeout, Promise, Error,
    JSON, String, Number, Array, Object, Date, Math, RegExp, Boolean,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return ctx;
}

// 在 vm 沙箱里求值一个表达式，拿到纯函数的返回值
function run(ctx, expr) {
  return vm.runInContext(expr, ctx);
}

(async () => {
  let fails = 0;
  const check = (name, cond, extra = "") => {
    console.log(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  — " + extra : ""}`);
    if (!cond) fails++;
  };
  const ctx = makeCtx();

  // ------------------------------------------------------------ penBadge
  const active = run(ctx, "penBadge('active')");
  const expired = run(ctx, "penBadge('expired')");
  const revoked = run(ctx, "penBadge('revoked')");
  check("生效中徽章 ≠ 已到期徽章", active !== expired, active);
  check("已到期徽章 ≠ 已撤销徽章", expired !== revoked);
  check("生效中带 L3 高亮", active.includes("L3") && active.includes("生效中"), active);
  check("已到期徽章是中性色", expired.includes("neutral") && expired.includes("已到期"), expired);
  check("未知状态兜底不崩", run(ctx, "penBadge('weird')").includes("weird"));

  // ---------------------------------------------------------- paintActions
  // 买家板块：有「平台介入」escalate 按钮
  const buyerActs = run(ctx, `paintActions([
    {kind:"escalate", label:"申请平台介入", title:"转平台介入"},
    {kind:"link", label:"我的工单", href:"/service?tab=tickets"}
  ])`);
  check("买家板块有 escalate 按钮", buyerActs.includes("phEscalate") && buyerActs.includes("平台介入"), buyerActs.slice(0, 40));
  check("链接动作渲染为 <a>", buyerActs.includes("<a ") && buyerActs.includes("/service"), "");

  // 运营/治理板块：不给 escalate —— 「页面上有没有按钮」由数据决定
  const govActs = run(ctx, `paintActions([
    {kind:"link", label:"仲裁台", href:"/platform"},
    {kind:"link", label:"规则库", href:"/rules"}
  ])`);
  check("治理板块没有 escalate 按钮", !govActs.includes("phEscalate") && !govActs.includes("平台介入"), govActs.slice(0, 40));
  check("治理板块有仲裁台/规则库链接", govActs.includes("仲裁台") && govActs.includes("规则库"));

  // 空动作数组：不渲染任何按钮（不是渲染一个空按钮）
  check("空动作数组不产出按钮", run(ctx, "paintActions([])") === "", "");

  // 转义：动作标题里的 HTML 不该被当标签渲染（防注入）
  const xss = run(ctx, `paintActions([{kind:"link", label:"<img src=x>", href:"#" }])`);
  check("动作 label 被转义（防注入）", !xss.includes("<img"), xss);

  // ---------------------------------------------------------- riskBadge
  check("L3 强制转人工", run(ctx, "riskBadge('L3')").includes("强制转人工"));
  check("L1 可直接回答", run(ctx, "riskBadge('L1')").includes("可直接回答"));
  check("空等级返回空串", run(ctx, "riskBadge('')") === "");

  // ---------------------------------------------------------- esc / fmtFull
  check("esc 转义尖括号", run(ctx, "esc('<b>x</b>')").includes("&lt;b&gt;"));
  check("fmtFull 把 T 换成空格", run(ctx, "fmtFull('2026-10-30T16:00:00')") === "2026-10-30 16:00:00");
  check("fmtFull 空值给占位符", run(ctx, "fmtFull('')") === "—");

  console.log(fails === 0 ? "\n全部通过" : `\n${fails} 项失败`);
  process.exit(fails === 0 ? 0 : 1);
})();
