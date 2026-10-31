"""截 v8 两个改动的真实页面：知识归属、订单商品明细与运单号。

沿用 tools_shot_boards.py 的 CDP 方案（headless --screenshot 对异步页面靠不住），
区别只是步骤：先点页签、再点列表第一行，等详情渲染完再截。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import httpx
import websocket

ROOT = Path(__file__).resolve().parent.parent      # 本文件在 tools/ 子目录下，ROOT 取上上级
PORT = 8019
BASE = f"http://127.0.0.1:{PORT}"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DBG = 9224
OUT = ROOT / "logs"


def wait_up(url: str, timeout: int = 90) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=2).status_code in (200, 404):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


class Tab:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=25)
        self.mid = 0

    def call(self, method: str, params: dict | None = None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method,
                                 "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result")

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                           "returnByValue": True})
        return r.get("result", {}).get("value")

    def shot(self, path: Path):
        r = self.call("Page.captureScreenshot", {"format": "png",
                                                 "captureBeyondViewport": False})
        path.write_bytes(base64.b64decode(r["data"]))
        print("  shot ->", path.name, path.stat().st_size // 1024, "KB")


def main() -> int:
    env = {**os.environ, "DB_BACKEND": "sqlite"}
    snap = ROOT / "data" / "_shot_v8.db"
    shutil.copy(ROOT / "data" / "rules.db", snap)
    env["SQLITE_PATH"] = str(snap)
    OUT.mkdir(exist_ok=True)
    log = open(OUT / "_shot_v8.log", "w", encoding="utf-8")
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    prof = ROOT / "logs" / f"_prof_v8_{int(time.time())}"
    edge = subprocess.Popen(
        [EDGE, "--headless=new", f"--remote-debugging-port={DBG}",
         f"--user-data-dir={prof}", "--no-first-run", "--disable-gpu",
         "--hide-scrollbars", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tab = None
    try:
        if not wait_up(f"{BASE}/api/health"):
            print("服务未起")
            return 2
        if not wait_up(f"http://127.0.0.1:{DBG}/json/version", 40):
            print("浏览器调试端口未起")
            return 2
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{DBG}/json").read())
        page = [t for t in tabs if t["type"] == "page"][0]
        tab = Tab(page["webSocketDebuggerUrl"])
        tab.call("Page.enable")
        tab.call("Runtime.enable")
        tab.call("Emulation.setDeviceMetricsOverride",
                 {"width": 1360, "height": 900, "deviceScaleFactor": 1, "mobile": False})

        def login(user: str, pwd: str):
            tab.call("Page.navigate", {"url": f"{BASE}/login"})
            time.sleep(2.0)
            code = tab.js(f"""
              (async () => {{
                const r = await fetch('/api/auth/login', {{method:'POST',
                  headers:{{'Content-Type':'application/json'}},
                  body: JSON.stringify({{username:'{user}', password:'{pwd}'}})}});
                return r.status;
              }})()""")
            print("  login", user, code)

        def go(url: str, wait: float = 5.0):
            tab.call("Page.navigate", {"url": url})
            time.sleep(wait)

        # 1) 商家：知识库按归属
        login("fengxia", "123456")
        go(f"{BASE}/service")
        tab.js("document.querySelector('.tab[data-tab=\"faq\"]').click()")
        time.sleep(3)
        tab.shot(OUT / "_shot_v8_faq_merchant.png")

        # 2) 运营：知识库跨店视图 + 归属筛选
        login("ops", "Ops@12345")
        go(f"{BASE}/service")
        tab.js("document.querySelector('.tab[data-tab=\"faq\"]').click()")
        time.sleep(3)
        tab.shot(OUT / "_shot_v8_faq_ops.png")

        # 3) 买家：咨询窗口的知识构成（本店专属 + 平台通用）
        login("lichen", "123456")
        go(f"{BASE}/cs", 7)
        tab.shot(OUT / "_shot_v8_cs_buyer.png")

        # 4) 买家：纠纷详情里的订单事实（商品 + 运单）
        go(f"{BASE}/dispute", 5)
        tab.js("document.querySelector('#list .it')?.click()")
        time.sleep(3)
        tab.shot(OUT / "_shot_v8_dispute_order.png")

        # 5) 运营：仲裁台订单事实
        login("ops", "Ops@12345")
        go(f"{BASE}/platform", 5)
        tab.js("document.querySelector('.tab[data-tab=\"arb\"]').click()")
        time.sleep(3)
        tab.js("document.querySelector('#pane-arb .dp-item')?.click()")
        time.sleep(3)
        tab.shot(OUT / "_shot_v8_platform_order.png")
    finally:
        try:
            tab.ws.close()
        except Exception:
            pass
        edge.terminate()
        srv.terminate()
        for p in (edge, srv):
            try:
                p.wait(timeout=8)
            except Exception:
                p.kill()
        log.close()
        shutil.rmtree(prof, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
