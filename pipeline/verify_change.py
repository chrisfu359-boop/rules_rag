"""变更验证：一次跑完 P11 要求的三件事里的前两件（第三件由 change_pipeline --inject-bad 验）。

  ① 改一条条款 -> 旧版标 expired（不删）+ 新版 version+1
  ② 改前问一次命中缓存、改后同问返回新内容

为什么要一个专门的脚本，而不是"我在终端里手敲了几行"：
  缓存验证必须走**和 /api/chat 完全一样的 key 公式**才有意义。
  手敲的 key 和线上不一样，验的就只是自己的手稿。

为什么在同一进程里跑前后两段：
  本机没有 Redis，进程内存缓存不跨进程共享。跨进程再问必然是 miss，
  那样根本看不出是"版本号把它隔离了"还是"进程重开了"。
  同一进程内，缓存一直活着，key 前缀一变立刻 miss —— 这才是版本号在起作用。

用法：
  python pipeline/verify_change.py --doc 商家售后服务管理规范 --query "已发货买家申请仅退款我多久处理"
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import common
import service.cache as cache
import service.qa as qa
from generation.llm import get_llm
from retrieval.bm25 import BM25
from retrieval.hybrid import HybridRetriever
from retrieval.rerank import Reranker
from retrieval.vector_store import get_vector_index


def build():
    docs = common.load_clauses()
    vi = get_vector_index()
    vi.load()
    rr = Reranker(enabled=False)
    idx = HybridRetriever(BM25(docs), vi, reranker=rr)
    qa.init(docs, idx, vi, rr, get_llm())
    return docs


def cache_key(q: str) -> str:
    """与 api/chat.py 完全一致的 key 公式。"""
    ver = cache.rule_version()
    model_tag = (qa.RT.llm.model or "template") if qa.RT.llm else "template"
    return f"rr:{ver}:chat:{hashlib.md5(model_tag.encode()).hexdigest()[:8]}:" \
           f"{hashlib.md5(q.encode()).hexdigest()}"


def ask(q: str) -> dict:
    ck = cache_key(q)
    res = cache.get(ck)
    hit = res is not None
    if not hit:
        res = qa.answer(q)
        cache.set(ck, res, ttl=600)
    top = (res.get("clauses") or [{}])[0]
    return {"hit": hit, "key": ck, "ver": cache.rule_version(),
            "clause": f"《{top.get('doc_name','-')}》{top.get('clause_no','-')}",
            "conclusion": (res.get("conclusion") or "").replace("\n", " ")[:150],
            "timeouts": res.get("timeouts") or []}


def show(tag: str, r: dict) -> None:
    print(f"  [{tag}] 规则版本={r['ver']}  {'命中缓存' if r['hit'] else '未命中(现算并写入)'}")
    print(f"         key={r['key']}")
    print(f"         引用={r['clause']}  时限={r['timeouts']}")
    print(f"         结论={r['conclusion']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--skip-pipeline", action="store_true", help="规则已改过，只做前后两次提问")
    a = ap.parse_args()

    print(f"\n{'=' * 78}\n阶段一：变更前，同一个问题问两次\n{'=' * 78}")
    build()
    q = a.query
    r1 = ask(q)
    show("1st", r1)
    r2 = ask(q)
    show("2nd", r2)
    print(f"  -> 第二次 {'命中' if r2['hit'] else '未命中'}，证明缓存链路本身是通的")

    if not a.skip_pipeline:
        print(f"\n{'=' * 78}\n阶段二：改原文 -> 跑流水线（apply / bump / rebuild / gate）\n{'=' * 78}")
        p = subprocess.run([sys.executable, "pipeline/change_pipeline.py", "--doc", a.doc,
                            "--reason", "verify_change 演练"],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        out = (p.stdout or "") + (p.stderr or "")
        for line in out.splitlines():
            if any(k in line for k in ("[1/6", "[diff]", "[3/6", "[4/6", "[5/6", "[6/6",
                                        "+ ", "- ", "~ ", "[regression]", "[gate]",
                                        "[发布]", "[回滚]", "[rollback]", "[alert]")):
                print("  " + line.rstrip())
        print(f"  流水线退出码 {p.returncode}")

    print(f"\n{'=' * 78}\n阶段三：变更后，同一个问题再问两次\n{'=' * 78}")
    build()                      # 重新载入条款（版本号也从磁盘读回来了）
    r3 = ask(q)
    show("3rd", r3)
    r4 = ask(q)
    show("4th", r4)
    print(f"  -> 第三次 {'命中' if r3['hit'] else '未命中'}：key 前缀（规则版本）变了，旧缓存整体隔离")
    print(f"  -> 第四次 {'命中' if r4['hit'] else '未命中'}：新版本下缓存重新生效")
    print(f"\n  改前版本 {r1['ver']} / 改后版本 {r3['ver']}；"
          f"结论是否变化：{'是' if r1['conclusion'] != r3['conclusion'] else '否'}")
    print(f"{'=' * 78}\n")


if __name__ == "__main__":
    main()
