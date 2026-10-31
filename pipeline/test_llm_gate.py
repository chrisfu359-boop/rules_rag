"""LLM 生成层 + 引用校验闸门的离线测试。

不花一分钱、不依赖任何厂商：本地起一个 OpenAI 兼容的假服务，构造三种模型行为，
验证"接上模型"这件事不是只改个 import 就完了。

三个用例覆盖了真实上线后会遇到的三种情况：
  A 正常回答且引用正确        → 采纳，generator=llm:mock
  B 回答里编造了不存在的条款号 → 重试一次，仍编造 → 整条丢弃退回模板
  C 服务返回 500              → 超时/报错降级模板，接口不报错

跑法：python pipeline/test_llm_gate.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------- 假模型服务 ----------------
class Handler(BaseHTTPRequestHandler):
    mode = "ok"           # ok | fake_cite | error
    hits = 0

    def log_message(self, *a):        # 关掉访问日志，输出干净
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        Handler.hits += 1

        if Handler.mode == "error":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"mock upstream down"}}')
            return

        # 判断是否是"纠错重试"这一次调用：user 消息里带"不存在的条款编号"
        msgs = body.get("messages", [])
        is_retry = any("不存在的条款编号" in (m.get("content") or "") for m in msgs)

        if Handler.mode == "fake_cite":
            content = json.dumps({
                "conclusion": "依据《中华人民共和国广告法》第九十九条，禁止使用国家级最高级用语。",
                "notice": "以最新公布版本为准。",
                "handoff": False,
            }, ensure_ascii=False)
        else:
            # 真实召回集里会有《网络购买商品七日无理由退货暂行办法》第十八条
            content = json.dumps({
                "conclusion": ("商品退回所产生的运费由消费者承担。\n"
                               "依据《网络购买商品七日无理由退货暂行办法》第十八条，"
                               "经营者与消费者另有约定的，按照约定执行。"),
                "notice": "本条为部门规章，适用于网络购买商品七日无理由退货场景。",
                "handoff": False,
            }, ensure_ascii=False)

        resp = {
            "id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 80, "total_tokens": 380},
        }
        raw = json.dumps(resp, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_mock():
    srv = HTTPServer(("127.0.0.1", 8899), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------- 测试主体 ----------------
def main() -> int:
    srv = start_mock()
    os.environ.update({
        "LLM": "auto",
        "LLM_PROVIDER": "",
        "LLM_BASE_URL": "http://127.0.0.1:8899/v1",
        "LLM_API_KEY": "mock-key",
        "LLM_MODEL": "mock-model",
        "LLM_TIMEOUT": "10",
    })

    import common
    from generation import llm as llm_mod
    from generation.answer import compose_llm, verify_citations

    clauses = common.load_clauses()
    # 拿一条真实条款当召回集，模拟检索结果
    target = next(c for c in clauses if "第十八条" in str(c["clause_no"]) and "运费" in c["content"])
    recalled = [{**target, "score": 0.93},
                *[{**c, "score": 0.4} for c in clauses if c["id"] != target["id"]][:3]]

    q = "买家签收后申请七天无理由退货，退货运费谁承担？"
    rows = []

    for mode, expect_gen, desc in [
        ("ok", "llm:mock-model", "A 正常回答 + 引用正确"),
        ("fake_cite", "template", "B 编造条款号 → 拦截退模板"),
        ("error", "template", "C 上游 500 → 降级模板"),
    ]:
        Handler.mode = mode
        Handler.hits = 0
        llm_mod._client = None                   # 重建客户端，清掉失败计数
        t0 = time.time()
        out = compose_llm(q, recalled, "L1", "常规咨询", None, "after_sale")
        ms = round((time.time() - t0) * 1000)
        ok = out.get("generator") == expect_gen
        rows.append((desc, out.get("generator"), ok, Handler.hits, ms, out))
        print(f"[{'PASS' if ok else 'FAIL'}] {desc}")
        print(f"       generator={out.get('generator')}  模型调用={Handler.hits}次  {ms}ms")
        print(f"       引用校验={out.get('citation_verified')}  fake={out.get('fake_citations')}")
        print(f"       llm={out.get('llm')}")
        print(f"       结论: {(out.get('conclusion') or '')[:80].replace(chr(10), ' ')}")
        print()

    # 单独验一下校验函数本身：法条号与平台规则号两种编号都要认
    # 注意这里要现造两条"合法条款"当召回集，不能拿真实条款 —— 真实召回集里没有
    # "第九条"和"2.3.1"，用它去测等于在测"不存在的编号会不会被判编造"（当然会）。
    fake_pool = [{"clause_no": "第九条"}, {"clause_no": "2.3.1"}]
    ok1, f1 = verify_citations("依据《广告法》第九条与平台规则 2.3.1 处理。", fake_pool)
    ok2, f2 = verify_citations("依据《广告法》第九十九条处理。", fake_pool)
    ok3, f3 = verify_citations("依据《广告法》第九条处理。", fake_pool)
    print(f"[{'PASS' if ok1 else 'FAIL'}] 校验器认两种编号（法条 + 平台规则）", f"fake={f1}")
    print(f"[{'PASS' if not ok2 else 'FAIL'}] 校验器能抓出编造编号:", f2)
    print(f"[{'PASS' if ok3 else 'FAIL'}] 单条法条引用也能通过:", f3)

    srv.shutdown()
    passed = (sum(1 for r in rows if r[2]) == len(rows)) and ok1 and ok3 and not ok2
    print(f"\n{'='*50}\n{'全部通过' if passed else '有失败项'} | {sum(1 for r in rows if r[2])}/{len(rows)} 用例")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
