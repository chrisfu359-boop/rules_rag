"""用 CDP 截三个咨询板块的真实页面。

之前用 `--screenshot` 命令行截 /cs 一直失败（跳转版、iframe 版、各种
virtual-time 都试过），但 /service 同样方法能成功——说明不是页面问题，
是命令行模式在异步 fetch 还没回来时就截了图。

改用 remote debugging + Page.captureScreenshot：登录、导航、等页面自己
把会话建好，再截。这样截出来的是真的东西，不是 mock。
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
PORT = 8017
BASE = f"http://127.0.0.1:{PORT}"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DBG = 9223
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
        self.ws = websocket.create_connection(ws_url, timeout=20)
        self.mid = 0

    def call(self, method: str, params: dict | None = None, wait: bool = True):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method,
                                 "params": params or {}}))
        if not wait:
            return None
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result")

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "awaitPromise": True, "returnByValue": True})
        return r.get("result", {}).get("value")

    def shot(self, path: Path):
        r = self.call("Page.captureScreenshot", {"format": "png",
                                                 "captureBeyondViewport": False})
        path.write_bytes(base64.b64decode(r["data"]))
        print("  shot ->", path.name, path.stat().st_size // 1024, "KB")


def main() -> int:
    env = {**os.environ, "DB_BACKEND": "sqlite"}
    snap = ROOT / "data" / "_shot_board.db"
    shutil.copy(ROOT / "data" / "rules.db", snap)
    env["SQLITE_PATH"] = str(snap)
    OUT.mkdir(exist_ok=True)
    log = open(OUT / "_shot_board.log", "w", encoding="utf-8")
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    prof = ROOT / "logs" / f"_prof_{int(time.time())}"
    edge = subprocess.Popen(
        [EDGE, "--headless=new", f"--remote-debugging-port={DBG}",
         f"--user-data-dir={prof}", "--no-first-run", "--disable-gpu",
         "--hide-scrollbars", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_up(f"{BASE}/api/health"):
            print("服务未起")
            return 2
        if not wait_up(f"http://127.0.0.1:{DBG}/json/version", 40):
            print("浏览器调试端口未起")
            return 2
        tabs = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{DBG}/json").read())
        page = [t for t in tabs if t["type"] == "page"][0]
        tab = Tab(page["webSocketDebuggerUrl"])
        tab.call("Page.enable")
        tab.call("Runtime.enable")
        tab.call("Emulation.setDeviceMetricsOverride",
                 {"width": 1280, "height": 880, "deviceScaleFactor": 1,
                  "mobile": False})

        for user, pwd, name in (("lichen", "123456", "buyer"),
                                ("fengxia", "123456", "merchant"),
                                ("ops", "Ops@12345", "ops")):
            print(f"[{name}]")
            tab.call("Page.navigate", {"url": f"{BASE}/login"})
            time.sleep(2.5)
            ok = tab.js(f"""
              (async () => {{
                const r = await fetch('/api/auth/login', {{method:'POST',
                  headers:{{'Content-Type':'application/json'}},
                  body: JSON.stringify({{username:'{user}', password:'{pwd}'}})}});
                return r.status;
              }})()""")
            print("  login", ok)
            tab.call("Page.navigate", {"url": f"{BASE}/cs"})
            time.sleep(6)
            # 发一句话，让右侧链路面板有内容；截图才有说服力
            tab.js("""(() => {
              const c = document.querySelector('.qchip');
              if (c) c.click();
              return !!c;
            })()""")
            time.sleep(4)
            tab.shot(OUT / f"_shot_cs_{name}.png")
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
