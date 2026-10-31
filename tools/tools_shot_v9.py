"""v9 截图：三个板块的头顶动作 + 治理板块的「挑一单查条款」。

重点验证两件事：
  1. 只有买家板块右上角有「平台介入」，商家/运营没有；
  2. 治理板块最后一格是选单入口，点开是真实案件，选中后摊出事实与条款。
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

ROOT = Path(__file__).resolve().parent.parent
PORT = 8041
BASE = f"http://127.0.0.1:{PORT}"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DBG = 9241
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
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.mid = 0

    def call(self, method: str, params: dict | None = None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result")

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                           "returnByValue": True})
        if r.get("exceptionDetails"):
            print("  JS 异常:", str(r["exceptionDetails"])[:160])
        return r.get("result", {}).get("value")

    def shot(self, path: Path):
        r = self.call("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(r["data"]))
        print("  shot ->", path.name, path.stat().st_size // 1024, "KB")


def main() -> int:
    env = {**os.environ, "DB_BACKEND": "sqlite"}
    snap = ROOT / "data" / "_shot_v9.db"
    shutil.copy(ROOT / "data" / "rules.db", snap)
    env["SQLITE_PATH"] = str(snap)
    OUT.mkdir(exist_ok=True)
    log = open(OUT / "_shot_v9.log", "w", encoding="utf-8")
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    prof = ROOT / "logs" / f"_prof_v9_{int(time.time())}"
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
            time.sleep(1.8)
            code = tab.js(f"""(async () => {{
              const r = await fetch('/api/auth/login', {{method:'POST',
                headers:{{'Content-Type':'application/json'}},
                body: JSON.stringify({{username:'{user}', password:'{pwd}'}})}});
              return r.status;
            }})()""")
            print("  login", user, code)

        def open_cs(wait: float = 5.5):
            tab.call("Page.navigate", {"url": f"{BASE}/cs"})
            time.sleep(wait)
            return tab.js("""(() => ({
              scope: (window.BOARD || {}).scope,
              acts: [...document.querySelectorAll('#phActs a,#phActs button')]
                      .map(e => e.textContent.trim()),
              state: (document.querySelector('#phState') || {}).textContent,
              greet: (document.querySelector('#phBody') || {}).innerText.slice(0, 60)
            }))()""")

        login("lichen", "123456")
        print(" 买家:", open_cs())
        tab.shot(OUT / "_shot_v9_cs_buyer.png")

        login("fengxia", "123456")
        print(" 商家:", open_cs())
        tab.shot(OUT / "_shot_v9_cs_merchant.png")

        login("ops", "Ops@12345")
        print(" 运营:", open_cs())
        tab.shot(OUT / "_shot_v9_cs_ops.png")

        tab.js("document.querySelector('.qchip.pick').click()")
        time.sleep(2.5)
        print(" 案件候选:", tab.js("document.querySelectorAll('.case-item').length"))
        tab.shot(OUT / "_shot_v9_case_picker.png")

        tab.js("document.querySelector('.case-item').click()")
        time.sleep(3.5)
        print(" 状态栏:", tab.js("(document.querySelector('#phState')||{}).textContent"))
        print(" 回复:", (tab.js(
            "([...document.querySelectorAll('#phBody .b-txt')].pop()||{}).innerText") or "")[:200])
        tab.shot(OUT / "_shot_v9_case_reply.png")

        # 纠纷中心的「申请平台介入」同样只属于买家
        def dispute(tag: str):
            tab.call("Page.navigate", {"url": f"{BASE}/dispute"})
            time.sleep(4.0)
            vis = tab.js("""(() => {
              const b = document.querySelector('#btnNew');
              return b ? getComputedStyle(b).display !== 'none' : null;
            })()""")
            print(f" {tag} 纠纷中心 申请平台介入按钮可见: {vis}")
            tab.shot(OUT / f"_shot_v9_dispute_{tag}.png")

        login("lichen", "123456")
        dispute("buyer")
        login("ops", "Ops@12345")
        dispute("ops")
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
        snap.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
