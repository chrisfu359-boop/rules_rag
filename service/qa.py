"""问答服务层：检索 → 风险路由 → 生成 → 引用校验，一次完整链路。

为什么从 app.py 抽出来：
  接上用户系统后，问答有两个入口（老的 /api/ask 和新的 /api/chat/ask）。
  逻辑留在视图函数里，两份必然会分叉——改了一处忘了另一处，是这类项目最常见的退化。
  抽成 Runtime + answer() 之后，视图只负责鉴权、落库、组装响应。

Runtime 持有重资源（索引、向量库、精排、模型），由启动钩子填充一次。
"""
from __future__ import annotations

import re
import time

import common
from generation.answer import compose_llm
from generation.llm import get_llm
from nlp import router as nlp_router

# 注意：这个常量只作默认值。真正的规则库版本号来自 service.cache.rule_version()，
# 常量写死会被"规则更新后缓存不失效"这个故障踩中 —— 详见 cache.rule_version() 的注释。
RULE_VERSION = "v5"

# 单通道 BM25 下判"无依据"的查询词覆盖率门槛。
# RRF 只看名次：rank0 恒为 1/61 ≈ 0.016，触达不到 0.010 的低置信阈值，于是边界题全被硬答。
# 覆盖率 = 查询词命中语料词典的比例（同义词扩展之前），是词典层唯一能判"没关系"的信号。
# 标定（300 条评测集，实测）：域内题覆盖率 min=0.667；边界无答案题里可被词典层识别的
# 全部 ≤0.5，只有 3 条语义越界题（征信/专利/宠物登记）落在 0.667~0.75 —— 那几条需要
# 向量通道（BGE）才判得出来，纯词法到此为止。0.60 是 0.5 与 0.667 之间的干净分界。
NO_BASIS_COVERAGE = 0.60

# 多轮记忆轮数上限：/api/chat/ask 把最近 N 轮 user/assistant 消息作为上下文传给生成层，
# 让模型理解"那运费呢""这样能退吗"这类指代/省略主语。10 轮 = 20 条消息。
HISTORY_ROUNDS = 10

# 追问判定：短 + 带指代词，视为"省略主语/宾语的追问"，检索时用上一轮用户问题补全上下文。
_FOLLOWUP_RE = re.compile(r"那|这|它|他|她|呢|还|也|这样|这个|那个|那么|然后|如果|或者")
_FOLLOWUP_MAX_LEN = 15


def _resolve_query(q: str, history: list[dict] | None) -> str:
    """多轮指代消解：追问句用上一轮用户问题补全检索上下文。

    只对"短追问"生效——正常完整问题即使短、也不含指代词，不会被误判；
    含"那/这/呢/还/也"这类词的追问才拼接历史。只影响检索，不影响风险路由和回显 query。
    """
    if not history:
        return q
    if len(q) > _FOLLOWUP_MAX_LEN or not _FOLLOWUP_RE.search(q):
        return q
    last_user = next((h.get("content") or "" for h in reversed(history)
                      if (h or {}).get("role") == "user"), "").strip()
    if not last_user or last_user == q:
        return q
    return f"{last_user} {q}"


class Runtime:
    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.index = None
        self.vindex = None
        self.reranker = None
        self.llm = None
        self.rule_version = RULE_VERSION

    @property
    def ready(self) -> bool:
        return self.index is not None

    def low_conf_threshold(self) -> float:
        """开精排时分数是 0~1 概率，没开是 RRF 的 1/(k+rank) 量级。
        阈值必须跟着通道口径走，一套阈值打两种链路必然失效。

        RRF（k=60）的真实口径：两通道都命中 rank0 = 2/61 ≈ 0.033，单通道 rank0 = 1/61 ≈ 0.016。
        原来一律写死 0.02，于是在「向量编码器不可用、只剩 BM25」的降级链路上，
        连最相关的第一条都在 0.02 以下 —— 表现是**每个问题都回答"未检索到依据"**。
        系统看起来很谨慎，实际是阈值口径错了。阈值必须按实际活着的通道数取。
        """
        r = self.reranker
        if r is not None and getattr(r, "enabled", False) and not getattr(r, "_failed", False):
            return 0.30
        ch = getattr(self.index, "last_debug", {}).get("channels") or {}
        live = sum(1 for v in ch.values() if v) if ch else 2
        return 0.02 if live >= 2 else 0.010

    def stats(self) -> dict:
        return {
            "clauses": len(self.docs),
            "vector": self.vindex.stats() if self.vindex else {},
            "rerank": self.reranker.stats() if self.reranker else {"enabled": False},
            "llm": self.llm.stats() if self.llm else {"available": False},
            "rule_version": self.rule_version,
        }


RT = Runtime()


def init(docs, index, vindex, reranker, llm=None) -> None:
    import service.cache as _cache
    # 版本号只有一处来源（cache.rule_version），qa 不再自己存一份常量。
    RT.rule_version = _cache.rule_version()
    RT.docs = docs
    RT.index = index
    RT.vindex = vindex
    RT.reranker = reranker
    RT.llm = llm or get_llm()


def answer(query: str, scene: str | None = None,
           history: list[dict] | None = None) -> dict:
    """执行一次完整问答，不落库、不管会话——那是视图层的事。

    history: 最近几轮 [{"role": "user"/"assistant", "content": ...}]，
             由视图层（/api/chat/ask）从 t_chat_message 捞出来传入，
             用于多轮指代消解（"那运费呢"）：检索侧用上一轮问题补全，
             生成侧（LLM）把历史塞进 prompt；同时原样回传供前端展示。
    """
    t0 = time.time()
    q = (query or "").strip()
    if not q:
        raise ValueError("问题不能为空")

    parsed = nlp_router.detect(q)
    order = common.find_order(q)
    level, reason = nlp_router.risk(q, order)

    # 多轮指代消解：追问句用上一轮用户问题补全检索上下文。
    # 只影响检索命中，不影响风险路由（risk 仍看原始 q）与回显的 query。
    search_q = _resolve_query(q, history)

    # L3 也召回条款原文，只是不给结论：用户要看到"凭什么转人工"，不是一片空白
    hits = RT.index.search(search_q, top_k=5, scene=scene or parsed["scene"])
    clauses = [{**d, "score": s} for d, s in hits]

    thr = RT.low_conf_threshold()
    low_conf = bool(clauses) and clauses[0]["score"] < thr

    # 单通道 BM25 的 RRF 分数触达不到上面那个阈值（见 NO_BASIS_COVERAGE 注释），
    # 用查询词覆盖率补一条"无依据"判据。只在覆盖率可用时生效（BM25 通道总是可用）。
    cov = (RT.index.last_debug or {}).get("bm25_coverage")
    if cov is not None and cov < NO_BASIS_COVERAGE:
        low_conf = True

    res = compose_llm(q, clauses, level, reason, order, parsed["scene"],
                      low_conf=low_conf, history=history)
    res.update({
        "query": q,
        "risk_level": level,
        "scene": parsed["scene"],
        "state_in": parsed["state_in"],
        "order": order,
        "clauses": [{
            "id": c.get("id"), "doc_name": c["doc_name"], "clause_no": c["clause_no"],
            "clause_path": c.get("clause_path"), "content": c["content"],
            "risk_level": c.get("risk_level"), "scene": c.get("scene"),
            "effective_from": str(c.get("effective_from") or ""),
            "source_url": c.get("source_url"), "score": c["score"],
        } for c in clauses],
        "trace": {
            "channels": RT.index.last_debug.get("channels", {}),
            "rerank_ms": RT.reranker.last_ms if RT.reranker else 0,
            "low_conf_threshold": thr,
            "vector_backend": RT.vindex.stats().get("backend") if RT.vindex else None,
            "generator": res.get("generator"),
            "llm": res.get("llm", {}),
        },
        "latency_ms": int((time.time() - t0) * 1000),
        "rule_version": RT.rule_version,
        # 多轮记忆：回传最近几轮对话，前端可展示"本次参考了这些上下文"。
        "history": history or [],
        "history_rounds": sum(1 for h in (history or []) if (h or {}).get("role") == "user"),
    })
    return res


def log_qa(session_id: str, user_id: int | None, res: dict, cache_hit: bool = False) -> None:
    """审计落库。失败只打印，不影响返回——日志写不进去不该让用户拿不到答案。"""
    try:
        common.execute(
            """INSERT INTO t_qa_log (session_id, query, scene, risk_level,
                 retrieved_clause_ids, cited_clauses, answer, latency_ms, cache_hit,
                 generator, llm_ms)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (session_id, res["query"], res.get("scene"), res.get("risk_level"),
             ",".join(str(c["id"]) for c in res.get("clauses", []) if c.get("id")),
             ",".join(c["clause_no"] for c in res.get("clauses", [])),
             res.get("conclusion", ""), res.get("latency_ms", 0), 1 if cache_hit else 0,
             res.get("generator"), (res.get("llm") or {}).get("ms") or 0))
    except Exception as e:
        print("[qa] 审计写入失败:", str(e)[:120])
