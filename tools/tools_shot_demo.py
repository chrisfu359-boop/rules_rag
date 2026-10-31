"""演示页截图：L1 / L2 / L3 各一张，供验收与 README 使用。

用 Edge 的 headless 截图（--screenshot），不模拟点击：
页面支持 ?q=问题 直接提问，截图脚本因此不需要 CDP 交互。

坑：--user-data-dir 必须放到系统临时目录，**不能放项目目录**。
仓库里 logs/_prof_* 那 1.15GB 就是早期把 Edge profile 写进 logs/ 的后果。

用法：
  python tools/tools_shot_demo.py            # 起服务 -> 截三张 -> 停服务
  python tools/tools_shot_demo.py --keep     # 不关服务（服务已在跑时用）
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports" / "shots"
PORT = 8077
BASE = f"http://127.0.0.1:{PORT}"
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CASES = [
    ("L1", "平台内经营者是什么意思"),
    ("L2", "已发货买家申请仅退款我多久处理"),
    ("L3", "虚标原价会被罚多少钱"),
]


def edge() -> str:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("没找到 msedge.exe")


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def wait_up(url: str, timeout: float = 90) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.6)
    return False


def main() -> None:
    keep = "--keep" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    proc = None
    if port_free(PORT):
        env = dict(os.environ, DB_BACKEND="sqlite", VECTOR_BACKEND="npz",
                   RERANK="off", PYTHONIOENCODING="utf-8",
                   PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app",
                                 "--host", "127.0.0.1", "--port", str(PORT)],
                                cwd=str(ROOT), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[shot] 启动服务 :{PORT} ...")
    if not wait_up(BASE + "/api/health"):
        raise SystemExit("服务没起来")

    tmp = tempfile.mkdtemp(prefix="edge-shot-")
    try:
        for name, q in CASES:
            out = OUT / f"demo_{name}.png"
            url = f"{BASE}/demo?q={urllib.parse.quote(q)}"
            cmd = [edge(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                   "--no-first-run", "--no-default-browser-check",
                   f"--user-data-dir={tmp}", "--window-size=1120,1500",
                   "--virtual-time-budget=9000",
                   f"--screenshot={out}", url]
            t0 = time.time()
            subprocess.run(cmd, timeout=120, capture_output=True)
            ok = out.exists() and out.stat().st_size > 8000
            print(f"[shot] {name}  {q[:22]:<24} -> {out.name} "
                  f"{'OK' if ok else 'FAIL'} ({round(time.time()-t0,1)}s, "
                  f"{out.stat().st_size if out.exists() else 0} bytes)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if proc and not keep:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            print("[shot] 服务已停止")


if __name__ == "__main__":
    main()
