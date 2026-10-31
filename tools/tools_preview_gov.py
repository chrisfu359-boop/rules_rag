"""临时脚本：抓真实接口数据 → 离线渲染 platform/dispute 两页 → 截图看布局。

只做视觉校验，不参与运行。用完即删。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PV = WEB / "assets" / "_pv2"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 8013
BASE = f"http://127.0.0.1:{PORT}"


def grab() -> dict:
    """以平台运营身份抓一份真实响应。"""
    out = {}
    ops = httpx.Client(base_url=BASE, timeout=40)
    ops.post("/api/auth/login", json={"username": "ops", "password": "Ops@12345"})
    out["/api/auth/me"] = ops.get("/api/auth/me").json()
    for p in ("/api/gov/meta", "/api/gov/dashboard", "/api/gov/disputes",
              "/api/gov/appeals", "/api/gov/penalties", "/api/gov/shops"):
        r = ops.get(p)
        out[p] = r.json()
    items = out["/api/gov/disputes"]["items"]
    # 详情页会按列表第一条自动打开，所以每一条都要有详情，否则 mock 只命中一条
    for x in items[:8]:
        out[f"/api/gov/disputes/{x['id']}"] = ops.get(
            f"/api/gov/disputes/{x['id']}").json()
    ops.close()
    return out


def build(name: str, data: dict, tab: str = "") -> pathlib.Path:
    src = (WEB / f"{name}.html").read_text(encoding="utf-8")
    apijs = (WEB / "assets" / "api.js").read_text(encoding="utf-8")
    mock = ("\nconst _DATA = " + json.dumps(data, ensure_ascii=False) + ";\n"
            "const _MISS = [];\n"
            "window.fetch = async (u) => {\n"
            "  const s = String(u); const i = s.indexOf('/api/');\n"
            "  const path = i < 0 ? s : s.slice(i).split('?')[0];\n"
            "  if (_DATA[path] === undefined) { _MISS.push(path);\n"
            "    return new Response('{}', {status:200,\n"
            "      headers:{'content-type':'application/json'}}); }\n"
            "  return new Response(JSON.stringify(_DATA[path]), {status:200,\n"
            "    headers:{'content-type':'application/json'}});\n"
            "};\n"
            # 把未被 mock 的请求路径暴露到页面上，避免"数据没渲染"被误读成布局问题
            "window.addEventListener('load', () => setTimeout(() => {\n"
            "  const d = document.createElement('div');\n"
            "  d.style.cssText = 'position:fixed;left:0;bottom:0;z-index:99999;"
            "font:11px monospace;background:#111;color:#0f0;padding:2px 6px;opacity:.85';\n"
            "  d.textContent = 'MISS: ' + (_MISS.join(',') || 'none');\n"
            "  document.body.appendChild(d);\n"
            "}, 1200));\n")
    if tab:
        # 直接切到指定页签，省得每个页签都要点一次
        mock += (f"window.addEventListener('load', () => setTimeout(() => "
                 f"typeof switchTab === 'function' && switchTab('{tab}'), 900));\n")
    # 静态资源可能被浏览器/代理缓存，加时间戳强制回源，否则改完样式看不出变化
    src = src.replace('/assets/style.css', '/assets/style.css?v=%d' % int(time.time()))
    origin = '<script src="/assets/api.js"></script>'
    if origin not in src:
        raise RuntimeError(f"{name}.html 没有找到 api.js 引用，mock 无法注入")
    src = src.replace(origin, f"<script>{apijs}</script>\n<script>{mock}</script>")
    PV.mkdir(parents=True, exist_ok=True)
    out = PV / f"{name}{('_' + tab) if tab else ''}.html"
    out.write_text(src, encoding="utf-8")
    return out


def shot(name: str, w: int, h: int, out: pathlib.Path, wait_ms: int = 6000) -> bool:
    """每张图用独立 profile，并在浏览器退出后轮询等待文件落盘。

    两个坑都踩过：
    1. 复用同一个 user-data-dir，第二次启动会挂到已存在的浏览器进程直接返回，
       截图静默不生成；
    2. Edge 的 --screenshot 是子进程异步写盘，subprocess 返回时文件还没出现，
       立刻判断存在性会得到"失败但文件稍后出现"的假结论。
    """
    for attempt in range(3):
        profile = PV / f"pf_{name}_{attempt}"
        subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        f"--user-data-dir={profile}", "--no-first-run",
                        "--disable-extensions", "--disable-background-networking",
                        f"--window-size={w},{h}", f"--virtual-time-budget={wait_ms}",
                        f"--screenshot={out}", f"{BASE}/assets/_pv2/{name}.html"],
                       capture_output=True, timeout=180)
        size = 0
        for _ in range(40):                      # 最多等 20s 落盘
            if out.exists():
                s = out.stat().st_size
                if s and s == size:
                    break
                size = s
            time.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)
        if out.exists() and out.stat().st_size > 20000:
            return True
    return False


def main():
    env = {**os.environ, "DB_BACKEND": "sqlite"}
    log = open(ROOT / "logs" / "preview2.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host",
                             "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        data = grab()
        print("抓到接口:", list(data))
        for name, tab, w, h in [("platform", "", 1600, 1000),
                                ("platform", "arb", 1600, 1050),
                                ("platform", "pen", 1600, 1000),
                                ("platform", "shop", 1600, 900),
                                ("dispute", "", 1500, 980)]:
            page = name + (("_" + tab) if tab else "")
            build(name, data, tab)
            p = ROOT / f"logs/_shot_{page}.png"
            ok = shot(page, w, h, p)
            print("shot:", p.name, "ok" if ok else "FAILED",
                  p.stat().st_size if p.exists() else 0)
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
