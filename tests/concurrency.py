"""并发压测：HTTP 端到端延迟与吞吐（补齐"P50/P95=1/3ms 是进程内内核耗时"这个口径缺口）。

跑的是真实 HTTP 链路：uvicorn + SQLite 后端 + BM25 单通道 + 模板生成，
用 ThreadPoolExecutor 打 /api/ask（无鉴权、无限流的那支），快照库写入，
不动 data/rules.db。

用法：
  python tests/concurrency.py                # 默认 300 请求 / 20 并发
  python tests/concurrency.py 500 50         # 自定义 请求数 并发数
"""
from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORT = 8101
BASE = f"http://127.0.0.1:{PORT}"

# 覆盖不同链路：域内普通 / L3 转人工 / 边界无依据（拒答门）
QUERIES = [
    "什么时候发货？",
    "七天无理由退货怎么退？",
    "经营者违反本法会被罚多少钱？",
    "网购商品质量有问题怎么办？",
    "我想申请一个发明专利，流程要走哪些步骤？",
]


def wait_up(timeout: int = 90) -> bool:
    for _ in range(timeout):
        try:
            if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main() -> int:
    n_req = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    n_conc = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    env = {**os.environ, "DB_BACKEND": "sqlite", "RERANK": "off",
           "VECTOR_BACKEND": "npz", "SQLITE_PATH": str(ROOT / "data" / "_conc.db")}
    shutil.copy(ROOT / "data" / "rules.db", env["SQLITE_PATH"])
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    log = open(ROOT / "logs" / "concurrency.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)

    lat, errors, statuses = [], [], {}
    try:
        if not wait_up():
            print("服务未能在 90 秒内启动，见 logs/concurrency.log")
            return 2

        def hit(i: int) -> tuple[float, int]:
            q = QUERIES[i % len(QUERIES)]
            t0 = time.time()
            try:
                r = httpx.post(f"{BASE}/api/ask", json={"query": q}, timeout=30)
                return time.time() - t0, r.status_code
            except Exception:
                return time.time() - t0, -1

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=n_conc) as ex:
            for ms, code in ex.map(hit, range(n_req)):
                lat.append(ms * 1000)
                if code == 200:
                    statuses[code] = statuses.get(code, 0) + 1
                else:
                    errors.append(code)
                    statuses[code] = statuses.get(code, 0) + 1
        wall = time.time() - t0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
        try:
            os.remove(env["SQLITE_PATH"])
        except OSError:
            pass

    lat.sort()
    def pct(p: float) -> int:
        return round(lat[min(len(lat) - 1, int(len(lat) * p))])

    ok = n_req - len(errors)
    print(f"并发压测  |  {n_req} 请求 / {n_conc} 并发  |  SQLite+BM25+模板  |  真实 HTTP")
    print(f"{'=' * 60}")
    print(f"  成功           {ok}/{n_req}  ({100 * ok / max(n_req, 1):.1f}%)")
    print(f"  状态码分布     {statuses}")
    print(f"  吞吐           {n_req / wall:.1f} req/s  (总耗时 {wall:.1f}s)")
    print(f"  延迟 均值      {statistics.mean(lat):.1f} ms")
    print(f"  延迟 P50       {pct(0.50)} ms")
    print(f"  延迟 P95       {pct(0.95)} ms")
    print(f"  延迟 P99       {pct(0.99)} ms")
    print(f"  延迟 max       {lat[-1]:.1f} ms")
    print(f"{'=' * 60}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
