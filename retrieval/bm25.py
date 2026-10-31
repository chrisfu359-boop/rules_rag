"""BM25 检索通道（Demo 阶段替代向量召回）。

为什么不直接上 BGE-M3：本机 Python 3.13 未装 torch，装齐 FlagEmbedding 需要下载
数 GB 依赖。Demo 先用纯 Python BM25 验证"过滤→召回→引用"链路是否成立，
向量通道按 docs/design/04 §3.1 后续接入，接口保持不变（search(query, filters)）。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

import jieba

jieba.setLogLevel(20)

# 口语 → 规则术语，对应 docs/design/04 的 query_rewriter（此处先内联，后续抽到 nlp/query_rewriter.py）
SYNONYMS = {
    "仅退款": ["退款", "退还货款"],
    "退货退款": ["退货", "退款", "七日"],
    "七天无理由": ["七日", "无理由", "退货"],
    "极限词": ["最高级", "最佳", "国家级", "广告"],
    "被罚": ["罚款", "处罚"],
    "扣分": ["处罚", "记入信用档案"],
    "保证金": ["押金", "费用"],
    "买家": ["消费者"],
    "卖家": ["经营者"],
    "发货": ["交付", "快递", "运输"],
    "到账": ["退还", "费用"],
    "虚假宣传": ["虚假", "误导", "宣传"],
    "一家卖": ["不合理限制", "附加不合理条件"],
    "二选一": ["不合理限制", "附加不合理条件"],
    "强制商家": ["不合理限制", "附加不合理条件"],
    "仅退款": ["退款", "退还货款", "七日"],
}

# 停用词要包含虚词/情态词。踩过的坑：只放"的了和"这类，结果"只能"这种
# 低频虚词 df=1 被稀有词加权放大，把无关条款顶到第一位（广告法第十五条）。
STOP = set("的了和与及或者是在对为以等该其此这那有被将把就都也而及其之对于"
           "不能只能可以可能需要是否能否这个那个这些那些一家我们你们他们它里用做干"
           "什么怎么如何多少怎样吗呢啊")


def tokenize(text: str) -> list[str]:
    toks = [t for t in jieba.lcut(text) if t.strip() and t not in STOP and len(t) > 1]
    return toks


class BM25:
    def __init__(self, docs: list[dict], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.df: Counter = Counter()
        self.tf: list[Counter] = []
        self.len: list[int] = []
        for d in docs:
            toks = tokenize(d["content"] + " " + d["clause_path"] + " " + d["doc_name"])
            c = Counter(toks)
            self.tf.append(c)
            self.len.append(max(len(toks), 1))
            self.df.update(c.keys())
        self.N = len(docs)
        self.avg = sum(self.len) / max(self.N, 1)
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, c in enumerate(self.tf):
            for t, f in c.items():
                self.postings[t].append((i, f))
        # 查询词覆盖率：最近一次 search 里"查询词命中语料词典"的比例。
        # 单通道 BM25 下 RRF 分数恒为 1/(k+rank) 量级、触达不到低置信阈值，
        # 覆盖率是唯一能判"这题根本没依据"的词典信号（标定见 service.qa.NO_BASIS_COVERAGE）。
        self.last_coverage: float | None = None

    def idf(self, t: str) -> float:
        n = self.df.get(t, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))

    def search(self, query: str, top_k: int = 5, scene: str | None = None) -> list[tuple[dict, float]]:
        q = tokenize(query)
        # 覆盖率在**同义词扩展之前**算：扩展词会把覆盖率人为抬高，
        # 让"出境留学"这种本来没几个词命中的题看起来像有依据。
        self.last_coverage = (sum(1 for t in q if t in self.df) / len(q)) if q else 0.0
        for w, ex in SYNONYMS.items():
            if w in query:
                q.extend(ex)
        q = list(dict.fromkeys(q))
        scores: dict[int, float] = defaultdict(float)
        for t in q:
            for i, f in self.postings.get(t, []):
                dl = self.len[i]
                s = self.idf(t) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
                scores[i] += s

        # 短语命中加权：jieba 会把"平台内经营者"切成"平台/内/经营者"，
        # 导致定义类问题被切碎后召回到无关条款。这里直接用原串子串匹配兜住。
        phrases = [p for p in re.split(r"[？?，,。、\s]|什么|叫做|是指|如何|怎么|多少", query) if len(p) >= 3]
        if phrases:
            for i, d in enumerate(self.docs):
                body = d["content"]
                for p in phrases:
                    if p in body:
                        scores[i] = scores.get(i, 0) + 3.5
                        # 定义式命中再加权：让"本办法所称X，是指……"优先于泛泛提到 X 的条款
                        if re.search(re.escape(p) + r"[，,、]?\s*(是指|是)", body):
                            scores[i] += 4.0
                        break

        # 定义类问句：优先"所称……是指"的定义条款
        if re.search(r"什么(叫做|是|意思)|定义|含义|所称|是指", query):
            for i in list(scores.keys()):
                if "所称" in self.docs[i]["content"]:
                    scores[i] *= 1.8

        # 同义词按短语命中加权：扩展词常被 jieba 再切碎，落到索引里匹配不上，
        # 这里用原始短语做一次直接命中，补住"二选一/一家卖 → 不合理限制"这类映射。
        for w, exs in SYNONYMS.items():
            if w in query:
                for e in exs:
                    if len(e) >= 3:
                        for i, d in enumerate(self.docs):
                            if e in d["content"]:
                                scores[i] = scores.get(i, 0) + 2.0

        # 词覆盖率加权：命中不同查询词越多越相关。
        # 作用：短条款（如广告法第九条）不会因为词频低、篇幅短被长条款压过去。
        qset = set(q)
        if qset:
            for i in list(scores.keys()):
                hit = sum(1 for t in qset if self.tf[i].get(t))
                scores[i] += 3.2 * hit / len(qset)

        # 稀有词加权：df 很小的词（如"最高级""二选一"）鉴别力最强。
        # 权重按 idf 缩放而不是固定值 —— 否则"广告"这种高频词靠词频就能压过
        # "最高级"这种决定性词（广告法第十八条曾因此排在第九条前面）。
        for t in qset:
            if self.df.get(t, 999) <= 5:
                w = 1.4 * self.idf(t)
                for i, _f in self.postings.get(t, []):
                    scores[i] += w

        # 场景过滤：命中场景的条款加权（等价于先过滤再排序，只是软过滤）
        if scene:
            for i in list(scores.keys()):
                d = self.docs[i]
                if d.get("scene") and d["scene"].split(".")[0] == scene.split(".")[0]:
                    scores[i] *= 1.35
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return [(self.docs[i], round(s, 3)) for i, s in ranked]

    def bm25_stats(self) -> dict:
        return {"docs": self.N, "vocab": len(self.df), "avg_len": round(self.avg, 1)}
