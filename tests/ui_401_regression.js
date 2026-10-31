/* 回归测试：前端 401 处理不能造成刷新死循环。
 *
 * 背景：登录页会探测 /api/auth/me 判断是否已登录，未登录时它返回 401。
 * 早期 api.js 对 401 无条件跳转 /login，于是「探测 → 401 → 跳登录页 → 再探测」
 * 变成无限刷新，页面一直闪、按钮点不到。
 *
 * 运行（需要服务已在 8000 端口）：
 *     node tests/ui_401_regression.js
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "web", "assets", "api.js"), "utf8");
const BASE = process.env.BASE_URL || "http://127.0.0.1:8000";

function makeCtx(pathname) {
  const loc = {
    pathname, search: "", href: "", navigations: [],
    replace(u) { this.href = u; this.navigations.push(u); },
  };
  const ctx = {
    location: loc,
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
    fetch: (p, o) => globalThis.fetch(/^https?:/i.test(p) ? p : BASE + p, o),
    console, setTimeout, clearTimeout, Promise, Error,
    JSON, String, Number, Array, Object, Date, Math, RegExp, Boolean,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return ctx;
}

async function call(ctx, p, opt) {
  try {
    await vm.runInContext(`api(${JSON.stringify(p)}, ${JSON.stringify(opt || {})})`, ctx);
    return "resolved";
  } catch (e) {
    return "threw:" + (e.status === undefined ? "?" : e.status);
  }
}

(async () => {
  let fails = 0;
  const check = (name, cond, extra = "") => {
    console.log(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`);
    if (!cond) fails++;
  };

  // 1. 登录页探测登录态，未登录返回 401 —— 绝不能跳转
  {
    const ctx = makeCtx("/login");
    const r = await call(ctx, "/api/auth/me", { quiet: true, noAuthRedirect: true });
    check("登录页探测不跳转", ctx.location.navigations.length === 0,
      `result=${r} navigations=${ctx.location.navigations.length}`);
    check("探测确实命中了真实接口（返回 401）", r === "threw:401", `result=${r}`);
  }

  // 2. 调用方漏传 noAuthRedirect 时，/login 上也要兜住
  {
    const ctx = makeCtx("/login");
    await call(ctx, "/api/auth/me", { quiet: true });
    check("登录页兜底不跳转", ctx.location.navigations.length === 0);
  }

  // 3. 业务页会话过期 —— 必须跳一次，且带上 next
  {
    const ctx = makeCtx("/app");
    await call(ctx, "/api/chat/sessions", {});
    const n = ctx.location.navigations;
    check("工作台过期跳转一次并带 next",
      n.length === 1 && String(n[0]).startsWith("/login?next="), `-> ${n[0]}`);
  }

  // 4. 并发请求同时 401 —— 只跳一次，避免重定向风暴
  {
    const ctx = makeCtx("/app");
    await Promise.all([
      call(ctx, "/api/chat/sessions", {}),
      call(ctx, "/api/auth/me", { quiet: true }),
      call(ctx, "/api/admin/metrics", {}),
    ]);
    check("并发 401 只跳一次", ctx.location.navigations.length === 1,
      `navigations=${ctx.location.navigations.length}`);
  }

  console.log(fails === 0 ? "\n全部通过" : `\n${fails} 项失败`);
  process.exit(fails === 0 ? 0 : 1);
})();
