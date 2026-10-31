"""端到端验证：服务 + 假模型，确认"填了 API 就能接上"这条链路是通的。

和 test_llm_gate.py 的区别：
  那个测的是生成函数（compose_llm），这个测的是 HTTP 接口（/api/ask → /api/llm/test），
  验证配置读取、缓存 key、审计落库、trace 透出这些"接口层"的东西也接对了。

跑法：python pipeline/e2e_mock.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app 之前设好：app.py 在模块级读环境变量
os.environ.update({
    "DB_BACKEND": "mysql",
    "RERANK": "off",              # 端到端验证与精排无关，关掉省 20 秒模型加载
    "LLM": "auto",
    "LLM_PROVIDER": "",
    "LLM_BASE_URL": "http://127.0.0.1:8899/v1",
    "LLM_API_KEY": "mock-key",
    "LLM_MODEL": "mock-model",
    "LLM_TIMEOUT": "15",
    "API_KEY": "",                # 演示态免鉴权，便于直接打接口
})

from pipeline.test_llm_gate import Handler, start_mock   # noqa: E402

srv = start_mock()
Handler.mode = "ok"

import uvicorn                                            # noqa: E402
import app as app_mod                                     # noqa: E402

port = 8001
server = uvicorn.Server(uvicorn.Config(app_mod.app, host="127.0.0.1", port=port,
                                       log_level="error"))
threading.Thread(target=server.run, daemon=True).start()

# 等启动完成（要加载 406 条条款 + 向量索引，实测 8~15 秒）
import requests                                           # noqa: E402

base = f"http://127.0.0.1:{port}"
for _ in range(60):
    try:
        if requests.get(f"{base}/api/health", timeout=2).status_code == 200:
            break
    except Exception:
        pass
    time.sleep(1)
else:
    print("[FAIL] 服务未启动")
    sys.exit(1)

rows = []


def check(name: str, cond: bool, detail: str = ""):
    rows.append(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


# 1) 配置检查接口
r = requests.post(f"{base}/api/llm/test", timeout=30).json()
check("模型连通性接口可用", r.get("ok") is True, f"model={r.get('model')} {r.get('ms')}ms")

# 2) 正常问答走模型
d = requests.post(f"{base}/api/ask", timeout=60,
                  json={"query": "买家签收后申请七天无理由退货，退货运费谁承担？",
                        "session_id": "e2e1"}).json()
check("问答由模型生成", str(d.get("generator", "")).startswith("llm:"),
      f"generator={d.get('generator')}")
check("trace 透出模型信息", bool((d.get("trace") or {}).get("llm")),
      str((d.get("trace") or {}).get("llm"))[:80])
check("引用校验通过", d.get("citation_verified") is True)

# 3) 同一问题再问一次 → 命中缓存，且不重复调模型
hits_before = Handler.hits
d2 = requests.post(f"{base}/api/ask", timeout=60,
                   json={"query": "买家签收后申请七天无理由退货，退货运费谁承担？",
                         "session_id": "e2e1"}).json()
check("二次提问命中缓存", d2.get("cache_hit") is True, f"{d2.get('latency_ms')}ms")
check("缓存命中不再调模型", Handler.hits == hits_before,
      f"模型调用次数 {hits_before}→{Handler.hits}")

# 4) 模型开始编造条款号 → 必须被拦下退回模板，接口不能 500
Handler.mode = "fake_cite"
d3 = requests.post(f"{base}/api/ask", timeout=60,
                   json={"query": "商品标题写国家级最高级会被罚多少钱？",
                         "session_id": "e2e2"}).json()
check("编造引用被拦截（未返回 500）", isinstance(d3, dict) and "generator" in d3,
      f"generator={d3.get('generator')}")

srv.shutdown()
print(f"\n{'='*50}\n{sum(rows)}/{len(rows)} 通过")
sys.exit(0 if all(rows) else 1)
