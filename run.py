"""一键启动器。

为什么启动逻辑在 Python 而不在 .bat：
  cmd.exe 在中文 Windows 上按 GBK 解析 .bat。文件存成 UTF-8 时，中文字节会被
  错位读取，导致后面的行被吞掉——报错"xxx 不是内部或外部命令"，脚本根本走不到
  启动那一步。把中文和逻辑全部搬到 Python，.bat 保持纯 ASCII，这个问题从根上消失。

除了启动，这个脚本还负责三件"没有它就会卡住用户"的事：
  1. 端口 8000 被占用 → 直接结束占用进程，而不是吐一句 Errno 10048 就退出
  2. Docker 容器没起来 → 自动降级 SQLite + 本地向量，服务照样能开
  3. 服务就绪后自动打开浏览器（轮询端口，不是傻等固定秒数）
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8000
URL = f"http://127.0.0.1:{PORT}"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def out(msg: str = "") -> None:
    print(msg, flush=True)


def listening_pids(port: int = PORT) -> list[str]:
    """解析 netstat 找监听指定端口的 PID。

    不用 psutil.net_connections()：那个在 Windows 上偶尔需要管理员权限，
    而 netstat 是任何账户都能跑的。端口后面拼空格是为了区分 8000 和 18000。
    """
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                           errors="ignore", timeout=10)
    except Exception:
        return []
    pids: list[str] = []
    for line in (r.stdout or "").splitlines():
        if "LISTENING" in line and f":{port} " in line:
            parts = line.split()
            if parts and parts[-1].isdigit() and parts[-1] not in pids:
                pids.append(parts[-1])
    return pids


def proc_name(pid: str) -> str:
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, errors="ignore", timeout=8)
        first = (r.stdout or "").strip().splitlines()[0]
        return first.split(",")[0].strip('"')
    except Exception:
        return "unknown"


def free_port() -> bool:
    pids = listening_pids()
    if not pids:
        return True
    for pid in pids:
        name = proc_name(pid)
        out(f"  · 端口 {PORT} 被占用（PID {pid}，进程 {name}），正在结束…")
        subprocess.run(["taskkill", "/PID", pid, "/F"],
                       capture_output=True, text=True, errors="ignore")
    for _ in range(10):
        time.sleep(0.6)
        if not listening_pids():
            return True
    return False


def docker_has(container: str) -> bool:
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True, errors="ignore", timeout=8)
        return container in (r.stdout or "")
    except Exception:
        return False


def open_when_ready() -> None:
    """轮询端口，通了再开浏览器。固定 sleep 15 秒在慢机器上会打开错误页，
    在快机器上又白等。"""
    for _ in range(90):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                time.sleep(2.0)          # 端口通到能响应 HTTP 还有一两秒
                out("")
                out(f"  [√] 服务已就绪 → {URL}")
                out("      浏览器已自动打开。关掉本窗口即停止服务。")
                out("")
                webbrowser.open(URL)
                return
        except OSError:
            time.sleep(1)


def main() -> int:
    os.chdir(ROOT)
    out("=" * 62)
    out("  电商规则合规助手  rules_rag")
    out("=" * 62)

    if not free_port():
        out("")
        out(f"[×] 端口 {PORT} 释放失败，可能被系统进程占用。")
        out(f"    手动执行：taskkill /PID <PID> /F")
        return 1
    out(f"  · 端口 {PORT} 可用")

    # 依赖服务：缺了自动降级，而不是把用户挡在门外
    db_backend, vec_backend = "mysql", "milvus"
    if not docker_has("mysql8"):
        db_backend = "sqlite"
        out("  ! Docker 里的 mysql8 没起来 → 降级为 SQLite（数据同源，功能不变）")
    if not docker_has("milvus_standalone"):
        vec_backend = "npz"
        out("  ! Milvus 没起来 → 降级为本地向量文件（检索质量不变）")
    if db_backend == "mysql":
        out("  · MySQL 就绪")
    if vec_backend == "milvus":
        out("  · Milvus 就绪")

    os.environ["DB_BACKEND"] = db_backend
    os.environ["VECTOR_BACKEND"] = vec_backend
    os.environ.setdefault("RERANK", "on")

    out("  · 正在加载规则库与模型（首次约 15 秒，请稍候）")
    out("")

    threading.Thread(target=open_when_ready, daemon=True).start()

    try:
        import uvicorn
        import app as app_mod
        uvicorn.run(app_mod.app, host="127.0.0.1", port=PORT, log_level="warning")
        return 0
    except KeyboardInterrupt:
        out("\n  已停止。")
        return 0
    except Exception as e:
        out("")
        out(f"[×] 启动失败：{type(e).__name__}: {e}")
        out("")
        out("  排查顺序：")
        out("    1. 是否改过 .env 里的配置，改错了")
        out("    2. Python 环境是否还在 D:\\ana\\.conda\\envs\\edu_rag")
        out("    3. 把上面这段报错整段发出来")
        return 1


if __name__ == "__main__":
    sys.exit(main())
