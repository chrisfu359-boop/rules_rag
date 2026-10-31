"""高频问答库（FAQ）索引与归一化 BM25 打分。

为什么不用现成的 retrieval/bm25.BM25：
  那套是给"法规条款"用的，输出是无上界的原始分（实测 0.03~40 都出现过）。
  原始分上挂一个 0.88 的阈值是没有意义的——阈值必须和分数的口径绑定。
  这里重新实现一版，把分数**归一化到 [0,1]**，0.88 才有确定的业务含义。

归一化的推导（这是整个模块最需要讲清楚的一段）：
  标准 BM25 单项贡献为
      s(t) = idf(t) * f * (k1+1) / (f + k1*(1 - b + b*dl/avgdl))
  取一篇"参考文档"：长度恰为平均长度（dl = avgdl），每个查询词各出现一次（f = 1），则
      s_ref(t) = idf(t) * (k1+1) / (1 + k1) = idf(t)
  于是把 Σ idf(t) 定义为满分 ideal：
      sim = min(raw / ideal, 1.0)
  含义非常明确：
      sim = 1.00  查询里的每个词都在 FAQ 里命中了，且文档长度正常
      sim = 0.88  覆盖了约 88% 的 IDF 权重（长文档会被长度惩罚扣下来）
      sim = 0.40  只命中一小半关键词 —— 不该直接回答，应该交给大模型或人工
  也就是说 0.88 ≈ "IDF 加权词覆盖率达 88%"，这是一个可以拿去和运营对齐的指标，
  而不是拍脑袋的数字。

为什么索引只放 question + keywords，不放 answer：
  FAQ 的匹配对象是"问法"，不是"答法"。把答案也索引进去，
  任意一条答案里出现"退款"二字的长 FAQ 都会拿到高分，阈值直接失效。
  长文档还会带来长度惩罚，让真正的问题短句反而排不到第一。
"""
from __future__ import annotations

import math
import os
import re
import unicodedata
from collections import Counter, defaultdict

import jieba

import common
import service.cache as cache

jieba.setLogLevel(20)

K1, B = 1.5, 0.75
COV_W = 0.85              # 覆盖率权重，其余给 BM25 强度（只用来打破同分）
PHRASE_BONUS = 0.06       # 问句与 FAQ 问法互相包含时的补偿
THRESHOLD = float(os.getenv("CS_FAQ_THRESHOLD", "0.88"))   # 直出阈值，可用环境变量覆盖
SUGGEST = 0.55            # 只作为大模型参考上下文的候选下限

# 客服场景的虚词。比法规场景多一批"亲/哦/呢/呀"这类聊天语气词，
# 它们 df 极高、idf 趋近 0，留着只会稀释分母。
STOP = set("的了和与及或者是在对为以等该其此这那有被将把就都也而及其之对于"
           "不能只能可以可能需要是否能否这个那个这些那些一家我们你们他们它里用做干"
           "什么怎么如何多少怎样吗呢啊吧哦呀哈嘛啦呗"
           "亲您好请问麻烦一下帮我我想知道告诉")

# 口语 → 规范词。买卖双方的语言差异是客服检索最大的噪声源：
# 买家说"啥时候能到"，知识库里写的是"几天能到货"，字面零重叠。
SYNONYMS: dict[str, list[str]] = {
    "发货": ["出库", "寄出", "发出", "发走"],
    "到货": ["收到", "送达", "到", "几天能到", "什么时候到"],
    "退款": ["退钱", "返钱", "退给我", "钱退"],
    "退货": ["退回", "退掉", "寄回", "退回去"],
    "换货": ["换一个", "换个", "调换", "换新"],
    "发票": ["开票", "开发票", "票据", "报销"],
    "运费": ["邮费", "快递费", "包邮", "运费险"],
    "尺码": ["码数", "大小", "尺寸", "号", "穿多大"],
    "正品": ["真假", "假货", "授权", "防伪", "是不是真的"],
    "优惠": ["便宜", "折扣", "降价", "活动", "划算", "少点"],
    "保修": ["质保", "售后", "维修", "坏了"],
    "人工": ["真人", "转人工", "客服"],
    "地址": ["收货地址", "收货信息"],
    "快递": ["物流", "快递公司", "单号"],
}

# 开头的礼貌语。不剥掉的话，"亲，我想问一下发货时间" 里最有信息量的
# 是"发货时间"，但 jieba 会把"亲"单独切出来占一个位置，覆盖率被稀释。
_POLITE = re.compile(r"^(亲+|你好|您好|哈喽|hello|hi|在吗|在么|请问|麻烦问一下|问一下|"
                     r"我想问一下|想问下|老板|店家|客服在吗|有人吗)+", re.I)
# 询问语气的前后缀。它们表达的是"我在问"，不是"我问什么"。
# 不剥掉的话，"能不能便宜点" 和知识库里的"能便宜点吗" 差三个字，
# 二元组覆盖直接掉到 0.6，一句再正常不过的话就被判成"知识库没有"。
# 注意：这里只是归一化，两侧（查询和索引）走同一套规则，不会造成不对称。
_Q_HEAD = re.compile(r"^(能不能|可不可以|能否|可以不|可以|帮我|给我|麻烦|能|请)+")
_Q_TAIL = re.compile(r"(行不行|可以吗|能不能|吗|呢|吧|么|呀|啊|嘛|啦)+$")
_PUNCT = re.compile(r"[\s\u3000?？!！。，,.、~～…\-—_:：;；\"'“”‘’()（）\[\]【】{}|/\\*#@$%^&+=]+")


def norm(text: str) -> str:
    """归一化：全角转半角 → 小写 → 剥礼貌语 → 剥询问语气 → 去标点空白。

    这一步必须在分词之前做。同一个问题在 Redis 缓存里能不能命中，
    完全取决于这里是否稳定——"发货了吗？" 和 "发货了吗" 是同一个问题，
    如果归一化不彻底，缓存会退化成"只能命中一模一样的字符串"，命中率接近 0。
    """
    s = unicodedata.normalize("NFKC", (text or "").strip().lower())
    s = _PUNCT.sub("", s)
    prev = None
    while prev != s:
        prev = s
        s = _POLITE.sub("", s)
        s = _Q_HEAD.sub("", s)
        s = _Q_TAIL.sub("", s)
    # 全被剥掉时（比如单独一句"在吗"）退回原文，否则会变成空串直接判空
    return s or (text or "").strip()


def tokenize(text: str) -> list[str]:
    toks = [t for t in jieba.lcut(text) if t.strip() and t not in STOP and len(t) > 1]
    return list(dict.fromkeys(toks))


def bigrams(text: str) -> list[str]:
    """字符二元组。

    为什么在词之外还要二元组：客服问题极短（平均 6 个字），jieba 在短句上很不稳定——
    "什么时候发货" 切成「什么/时候/发货」，"啥时候发" 切成「啥/时候/发」，
    单字被长度过滤丢掉之后字面重叠只剩一个"时候"，召回直接崩。
    二元组不看分词边界，短句上的召回稳定得多，代价是索引变大（这里 70 多条 FAQ 无所谓）。
    """
    s = norm(text)
    return [s[i:i + 2] for i in range(len(s) - 1)] if len(s) >= 2 else []


def feats(text: str) -> list[str]:
    """索引/查询共用的特征集：词 + 二元组。"""
    return list(dict.fromkeys(tokenize(text) + bigrams(text)))


class FaqIndex:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.df: Counter = Counter()
        self.tf: list[Counter] = []
        self.len: list[int] = []
        self.norm_q: list[str] = []

        for r in rows:
            nq = norm(r["question"])
            self.norm_q.append(nq)
            # 索引文本 = 问法 + 相似问法 + 关键词。答案不进索引（见模块 docstring）。
            # 相似问法（aliases）是客服知识库的标准结构：一条知识有一个标准问法，
            # 外加若干个买家真实说法。"我要退款" / "怎么退钱" / "退款流程"
            # 是同一条知识，靠公式去凑相似度不如直接把说法录进去。
            kws = [k for k in (r.get("keywords") or "").replace(",", " ").split() if k]
            alts = [a for a in (r.get("aliases") or "").split("|") if a.strip()]
            toks = list(dict.fromkeys(
                feats(nq) + [t for k in kws for t in feats(k)]
                + [t for a in alts for t in feats(a)]))
            c = Counter(toks)
            self.tf.append(c)
            self.len.append(max(len(toks), 1))
            self.df.update(c.keys())

        self.N = len(rows)
        self.avg = (sum(self.len) / self.N) if self.N else 1.0
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, c in enumerate(self.tf):
            for t, f in c.items():
                self.postings[t].append((i, f))

    def idf(self, t: str) -> float:
        n = self.df.get(t, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))

    # ---------------------------------------------------------- 打分
    def search(self, query: str, top_k: int = 5) -> list[dict]:
        nq = norm(query)
        orig = feats(nq)                      # 原始特征，决定覆盖率分母
        if not orig:
            return []

        # 同义词扩展只用来**加分**，不进分母。
        # 这点踩过一次：把扩展词算进 ideal 之后，"什么时候发货" 会同时要求
        # FAQ 里出现 出库/寄出/发出/发走 这些它本来就没有的说法，
        # 结果连标准问法自己也只有 0.45 分，0.88 变成永不命中的死阈值。
        syn: list[str] = []
        for w, exs in SYNONYMS.items():
            if w in nq or any(e in nq for e in exs):
                for x in [w] + exs:
                    syn.extend(feats(x))
        syn = [t for t in dict.fromkeys(syn) if t not in orig]

        idf_orig = {t: self.idf(t) for t in orig}
        ideal = sum(idf_orig.values())
        if ideal <= 0:
            return []

        raw: dict[int, float] = defaultdict(float)
        cover: dict[int, float] = defaultdict(float)
        hitn: dict[int, int] = defaultdict(int)
        for t in orig + syn:
            w = self.idf(t)
            for i, f in self.postings.get(t, []):
                dl = self.len[i]
                raw[i] += w * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / self.avg))
                if t in idf_orig:
                    cover[i] += idf_orig[t]
                    hitn[i] += 1

        out = []
        for i in raw:
            # 覆盖率用两个口径取平均：IDF 加权覆盖 + 命中词数占比。
            # 只用 IDF 加权会被稀有词劫持——查询里夹一个几乎不出现的词，
            # 只要命中它，分数就被顶到很高；只用词数占比又会让"的/了"这类
            # 高频二元组拉低真正相关的条目。两个取平均互相兜住。
            cov_idf = cover[i] / ideal
            cov_cnt = hitn[i] / len(orig)
            cov = min(0.5 * cov_idf + 0.5 * cov_cnt, 1.0)
            sat = min(raw[i] / ideal, 1.0)        # 含长度惩罚的 BM25 强度
            sim = COV_W * cov + (1 - COV_W) * sat
            # 短语级命中补偿：问句与 FAQ 问法互相包含时基本不会错。
            # 纯词袋会低估这种情况（分词边界不同），这一脚是给"完全同义改写"兜底的。
            a, b = nq, self.norm_q[i]
            if len(a) >= 3 and (a in b or b in a):
                sim = min(sim + PHRASE_BONUS, 1.0)
            if sim <= 0:
                continue
            out.append({**self.rows[i], "sim": round(sim, 4), "raw": round(raw[i], 3),
                        "cov": round(cov, 3), "cov_idf": round(cov_idf, 3),
                        "cov_cnt": round(cov_cnt, 3), "sat": round(sat, 3),
                        "matched": [t for t in orig if self.tf[i].get(t)][:8]})

        # 排序第三键是 _own：同一问法本店和平台各有一条、分数打平时，本店口径优先。
        # （完全同问法的情况已被 rows_from_db 覆盖掉，这里兜住"问法不同但都能命中"的情形）
        out.sort(key=lambda x: (-x["sim"], -int(x.get("_own") or 0),
                                -(x.get("priority") or 0), -(x.get("hit_count") or 0)))
        return out[:top_k]

    def best(self, query: str) -> tuple[dict | None, list[dict]]:
        """返回 (是否命中阈值的 top1, 前若干候选)。候选永远返回，供大模型兜底时做参考。"""
        cands = self.search(query, top_k=5)
        if cands and cands[0]["sim"] >= THRESHOLD:
            return cands[0], cands
        return None, cands

    def stats(self) -> dict:
        own = sum(1 for r in self.rows if r.get("shop_id"))
        cat = sum(1 for r in self.rows if not r.get("shop_id") and (r.get("cat_scope") or ""))
        return {"faqs": self.N, "own": own, "cat": cat, "global": self.N - own - cat,
                "vocab": len(self.df), "avg_len": round(self.avg, 1),
                "threshold": THRESHOLD, "version": version()}


# ---------------------------------------------------------------- 全局索引（域 × 店铺）
#
# 索引表本身在下面「索引（域 × 店铺）」一节声明，这里只放版本号。

_VER = 0


def version() -> int:
    """知识库版本号。缓存 key 必须带上它——FAQ 改过之后旧答案就该作废。

    单实例用进程内计数就够；多实例要靠 Redis 共享。两者取 max：
    Redis 被 flush 过会回到 0，取 max 才不会让版本号倒退回旧值。
    """
    v = _VER
    if cache.RC:
        try:
            rv = int(cache.RC.get("rr:cs:faqv") or 0)
            v = max(v, rv)
        except Exception:
            pass
    return v


def bump() -> int:
    global _VER
    _VER += 1
    if cache.RC:
        try:
            cache.RC.incr("rr:cs:faqv")
        except Exception:
            pass
    return _VER


# ---------------------------------------------------------------- 索引（域 × 店铺）

# 三个知识域。索引必须按域分开建，不能建一个大索引再过滤结果：
# 过滤是"先取 top5 再筛"，一旦别的域的条目排进前 5，本域正确的答案就被挤出去了，
# 用户看到的是"知识库没有这条"。这是分域检索最容易写错的地方。
AUDIENCES = ("buyer", "merchant", "ops")
DEFAULT_AUDIENCE = "buyer"

# 索引键是 (知识域, 店铺)。分域解决了「谁在问」，分店铺解决「问的是哪家店」。
# 两者都不能合并成大索引再过滤——原因同上，top5 会被别的域/别的店挤占。
#   第 1 维 audience：buyer / merchant / ops
#   第 2 维 shop_id ：None = 平台通用（规则域、治理域永远是 None）
#                    int  = 该店铺的视野（平台通用 + 本店专属合并成一份索引）
_IDX: dict[tuple[str, int | None], FaqIndex] = {}


def rows_from_db(audience: str = DEFAULT_AUDIENCE,
                 shop_id: int | None = None) -> list[dict]:
    """取某个域、某个店铺视野下的知识条目。

    shop_id=None 只取平台通用条目（规则域/治理域的正常情况）；
    shop_id=N 取「平台通用 + 类目模板 + 本店专属」三层，并做**覆盖合并**：
    同一问法按 本店 > 类目 > 平台 的优先级胜出，低优先级的同名条目丢掉。

    为什么要在索引层做覆盖，而不是靠 priority 打分排序：
    两条问法一字不差时 BM25 分数完全相等，排序靠 priority 兜底，
    这只能保证"排第一"，两条都还在候选里，大模型兜底时会把低优先级口径也读进去。
    覆盖合并是语义级的：本店说过的话，平台/类目就不该再插嘴。

    类目模板解决的是「开新店」的边际成本：同品类店铺的通用承诺（发货时效、
    退换政策）由平台按类目维护一份，新店一注册就继承，只有差异化的部分才需要
    本店自己录。没有这一层，每开一家店都要把整套话术抄一遍。
    """
    if shop_id is None:
        # 平台基线只含「全平台通用」条目（cat_scope 为空）。类目模板是
        # 给某品类店铺用的，进这里会让规则域/治理域（也走 shop_id=None）
        # 和"平台通用"口径混进类目模板，语义就乱了。
        return common.fetch_all(
            """SELECT id, question, answer, category, audience, shop_id, cat_scope,
                      keywords, aliases, priority, status, hit_count, source, updated_at
               FROM t_faq WHERE status='enabled' AND audience=%s AND shop_id IS NULL
                 AND (cat_scope='' OR cat_scope IS NULL)
               ORDER BY priority DESC, id ASC""", (audience,))

    # 查这家店的类目，用于决定「类目模板」这一层取哪几条。
    # 查不到类目就退化成两层（平台通用 + 本店专属），语义与 v8 完全一致。
    shop_cat = ""
    try:
        cat_rows = common.fetch_all(
            "SELECT category FROM t_shop WHERE id=%s", (shop_id,))
        shop_cat = (cat_rows[0].get("category") or "") if cat_rows else ""
    except Exception:
        shop_cat = ""

    rows = common.fetch_all(
        """SELECT id, question, answer, category, audience, shop_id, cat_scope,
                  keywords, aliases, priority, status, hit_count, source, updated_at
           FROM t_faq
           WHERE status='enabled' AND audience=%s
             AND (shop_id=%s
                  OR (shop_id IS NULL AND (cat_scope='' OR cat_scope=%s)))
           ORDER BY (CASE WHEN shop_id IS NOT NULL THEN 2
                          WHEN cat_scope<>'' THEN 1
                          ELSE 0 END) DESC, priority DESC, id ASC""",
        (audience, shop_id, shop_cat))

    # 覆盖合并：三层里同名问法只保留优先级最高的一层。
    # _own 分级：2=本店专属，1=类目模板，0=平台通用。排序第三键用它，
    # 让「问法不同但都能命中」时本店口径也排在前面。
    best_tier: dict[str, int] = {}
    for r in rows:
        key = norm(r["question"])
        tier = 2 if r.get("shop_id") else (1 if (r.get("cat_scope") or "") else 0)
        if tier > best_tier.get(key, -1):
            best_tier[key] = tier

    out = []
    for r in rows:
        tier = 2 if r.get("shop_id") else (1 if (r.get("cat_scope") or "") else 0)
        if tier < best_tier.get(norm(r["question"]), -1):
            continue
        r["_own"] = tier
        out.append(r)
    return out


def load(audience: str = DEFAULT_AUDIENCE, shop_id: int | None = None) -> FaqIndex:
    _IDX[(audience, shop_id)] = FaqIndex(rows_from_db(audience, shop_id))
    return _IDX[(audience, shop_id)]


def index(audience: str = DEFAULT_AUDIENCE, shop_id: int | None = None) -> FaqIndex:
    """取某个知识域（可选某家店）的索引。

    域不存在时退化成空索引，而不是回落到买家域——
    回落等于"商家问了平台规则，系统拿店铺话术回答"，比不回答更糟。
    """
    key = (audience if audience in AUDIENCES else DEFAULT_AUDIENCE, shop_id)
    if key not in _IDX:
        return load(*key)
    return _IDX[key]


def shop_ids() -> list[int]:
    """所有店铺 id。重建索引时要按这个表来，不能只重建有专属条目的店——
    类目模板改了之后，依赖该类目模板、但自己没有专属条目的店也要跟着刷新。"""
    try:
        rows = common.fetch_all("SELECT id FROM t_shop ORDER BY id")
        return [int(r["id"]) for r in rows if r.get("id") is not None]
    except Exception:
        return []


def reload() -> dict[tuple[str, int | None], FaqIndex]:
    """改完知识库必须重建**所有**域 × 所有店铺。

    只重建当前这一份，别的店会继续用旧话术回答——而改知识的人恰恰是运营，
    他改完只会去自己的预览页看一眼，看不到别家店还在用旧答案。
    """
    bump()
    for a in AUDIENCES:
        _IDX.pop((a, None), None)
        load(a, None)
        for sid in shop_ids():
            load(a, sid)
    # 已经删光专属条目的店，把空壳索引也清掉，避免占用内存和残留旧条目
    live = {(a, s) for a in AUDIENCES for s in [None] + shop_ids()}
    for k in list(_IDX):
        if k not in live:
            _IDX.pop(k, None)
    return _IDX


def hit(faq_id: int) -> None:
    """命中计数。写失败不能影响这一次回复——统计是旁路，不是主链路。"""
    try:
        common.execute("UPDATE t_faq SET hit_count=hit_count+1 WHERE id=%s", (faq_id,))
    except Exception:
        pass
