# EduRAG 复用评估与改造清单

> 评估对象：`D:\hm_st\EduRAG\资料\完整代码\edu_rag`
> **本评估为只读。原目录未做任何修改。**
> 结论用于 `rules_rag`（电商商家规则合规助手）的工程选型。

---

## 1. 结论先行

**不能"完整套用"。**

它不是"一个能直接跑电商规则的 RAG"，它是"一个教育客服 RAG 的完整实现"。
两者的**检索单元**和**正确性定义**根本不同：

| | EduRAG | rules_rag |
|---|---|---|
| 检索单元 | 语义段落（1024/256 字符） | 法律条款（第X条） |
| 正确性定义 | 答案意思对 | 条款号对 + 原文准 + 版本对 |
| 失效机制 | 无 | 规则会改版，旧条款必须不召回 |
| 拒答 | "信息不足转人工" | 主动设计（高风险不给结论） |
| 目标 | 答得让人满意 | 答得能拿去申诉 |

但**工程骨架可以复用约 60%**，尤其是 BGE-M3 双向量 + Milvus 混合检索 + CrossEncoder 精排这条链路，
自己从零写要两周，这里已经跑通了。

正确姿态：**把它当"基础设施基线"，不当"业务实现"。**

---

## 2. 复用分级

### ✅ 原样复用（0 改动或只改配置值）

| 文件 | 行数 | 价值 | 说明 |
|---|---|---|---|
| `base/config.py` | 120 | 高 | env > ini > fallback 三级覆盖，设计干净。新增配置项即可，不要重写 |
| `base/logger.py` | — | 中 | 双输出（文件 + 控制台），直接可用 |
| `mysql_qa/cache/redis_client.py` | 79 | 中 | 连接与序列化封装，加"版本前缀"后可用 |
| `rag_qa/models/` | 6.4G | **最高** | bge-m3 / bge-reranker-large / bert-base-chinese 都是本地权重，不用重新下载 |

### ⚠️ 小改（骨架对，改参数或字段）

| 文件 | 行数 | 要改什么 |
|---|---|---|
| `edu_chinese_recursive_text_splitter.py` | 109 | 分隔符表加 `第[一二三四五六七八九十百零〇\d]+条` / `第X章` / `（一）`。这是**唯一需要动的切分引擎** |
| `mysql_client.py` | 126 | `jpkb` 表结构写死（subject_name/question/answer），换成规则表 |
| `app.py` | 294 | FastAPI + WebSocket 流式骨架可用，但见 §5 的 CORS 缺陷 |

### 🔧 留骨架、换逻辑（改动量中等，但必须动）

| 文件 | 行数 | 现状 | 必须改成 |
|---|---|---|---|
| `vector_store.py` | 253 | schema 只有 `id/text/dense/sparse/child_id/parent_id/parent_content/source/timestamp`；`expr` 只拼等值 `source == 'x'` | 加 `effective_from/effective_to/category/scene/state_in/risk_level` 字段；`expr` 支持多条件 AND + 范围比较 |
| `document_processor.py` | 120 | 父 1024 / 子 256 字符递归切分；metadata 只有 `source/file_path/timestamp` | 改为条款级切分；metadata 注入业务字段 |
| `new_rag_system.py` | 231 | 4 种检索策略（直接/HyDE/子查询/回溯）；context 是 `"\n\n".join(...)` 纯字符串 | 保留历史注入 + 4096 截断 + 流式；策略层换成确定性路由 |
| `bm25_search.py` | 107 | softmax 归一化 + 0.85 阈值 FAQ；缓存 key 为 `answer:{query}` | 阈值需重新标定（见 §5）；缓存 key 必须带规则库版本号 |
| `rag_assessment/` | 160 | RAGAS 四指标（忠实度/答案相关性/上下文精确率/召回率） | 只能当 baseline，覆盖不了"条款引用准确率""时效正确率" |

### ✍️ 必须新写（课程项目里完全没有）

| 模块 | 为什么必须有 | 依赖复用件 |
|---|---|---|
| `ingest/clause_splitter.py` | 保证条款不被切断，继承 章→节→条 路径 | 复用递归切分引擎 |
| `ingest/metadata.py` | 三态时效（公示/生效/失效）+ scene/state_in/actor | — |
| `retrieve/filters.py` | 构造多条件 expr（时效范围 + 类目 + 场景 + 风险） | 依赖 vector_store 扩展后的 schema |
| `generate/risk_router.py` | L1 直答 / L2 带免责 / L3 只给条款并强制转人工 | — |
| `generate/citation_verifier.py` | 生成后校验条款号 ∈ 召回集，否则降级拒答 | — |
| `ingest/change_pipeline.py` | content_hash 变更检测 → 旧版标失效 → 触发回归 | — |
| `eval/run_regression.py` | 改规则/prompt/模型必须过门禁 | — |

---

## 3. 四个根本性不兼容（这是"不能完整套用"的实质）

### 3.1 切分粒度 ≠ 引用粒度

EduRAG 按字符数切：父块 1024、子块 256。切分点是"句号/分号/逗号/空"。
规则库必须按"条"切。

**后果**：以《商家售后服务管理规范》2.3.1(二) 为例，"退货退款 36 小时"和"换货 36 小时"
在原文里相邻且句式几乎一样。256 字符切分极可能把两者切在同一块或跨块——
届时答案给不出准确条款号，而"给不出条款号"等于这个系统没有存在价值。

**这不是调参能解决的，是范式不同。**

### 3.2 父块去重会丢掉全部 metadata（最致命）

`vector_store.py` 第 236-243 行：

```python
def _get_unique_parent_docs(self, child_chunks):
    parent_contents = set()
    for chunk in child_chunks:
        parent_contents.add(chunk.metadata.get('parent_content', chunk.page_content))
    return [Document(page_content=content) for content in parent_contents]
```

用 `set` 去重父块正文，然后**新建 `Document` 只保留 `page_content`**。
父块级的 `parent_id` / `source` / `timestamp` 全部丢失。

**后果**：重排之后拿到的是"一段没有出处的文本"。而引用溯源全靠这些字段。
这一处必须改，改动量 20 行以内，但不改则整个引用功能无法实现。

### 3.3 单值 timestamp ≠ 三态时效

EduRAG 只有 `timestamp`（入库时间，VARCHAR(50)），且过滤是**等值比较**。

规则要求：`publish_date`（公示日）+ `effective_from` + `effective_to`，
过滤是**范围比较** `effective_from <= now < effective_to`。

Milvus 的 `expr` 支持这种比较，但 EduRAG 的 `AnnSearchRequest` 里写死了
`expr_str = f"source == '{source_filter}'"`，不会拼范围条件。
必须重写 expr 构造。

### 3.4 FAQ 快通道的语义不匹配

EduRAG 的 FAQ 是"高频问题 → 标准答案"直接返回，跳过 LLM（`bm25_search.search` 返 `answer, False`）。
规则场景**不能这么做**：

- 规则答案必须带条款原文 + 生效期，纯文本答案拿去申诉是无效的
- 规则一改版，缓存的答案就错了，而 `answer:{query}` 这个 key **不带版本号**

**保留快通道是对的**（高频标准问题不该每次都调 LLM），但缓存 key 必须变成
`answer:v{rule_base_version}:{query}`，且命中后仍要附加条款元信息。

> 这与短剧项目里踩过的「缓存不带版本就会被旧权重坑」是同一类问题。

---

## 4. 执行风险：内存

本机实测模型体积：

| 模型 | 体积 |
|---|---|
| `bge-m3` | 2.2G |
| `bge-reranker-large` | 2.1G |
| `bert-base-chinese` | 1.3G |
| `bert_query_classifier` | 391M |
| `nlp_bert_document-segmentation_chinese-base` | 389M |

EduRAG 的 `VectorStore.__init__` **在构造时就同时加载 bge-m3 与 bge-reranker-large**
（`CrossEncoder(..., device='cpu')` + `BGEM3EmbeddingFunction(..., device='cpu')`），
且 fp16 关闭（`use_f16=False`）。

在 15G 主机、可用内存常只剩 2–3G 的情况下，这一条链路的常驻内存约 4.5–6G，
再加 Milvus 容器、BERT 分类器，**大概率触发 `MemoryError: bad allocation`**。

**应对（按优先级）**：
1. `bge-reranker-large` 换 `bge-reranker-base`（体积约 1/3，中文场景差距通常在 1–2pp）
2. embedding 与 rerank **分进程**，或用 API 版 embedding
3. `VectorStore` 改为**懒加载**：reranker 首次用到才初始化
4. 若必须全本地，先 `nvidia-smi` 与任务管理器确认可用内存，再决定

---

## 5. 复用中发现的 4 个问题（建议在拷贝时一并修掉）

### 5.1 `app.py` 的 CORS 配置互斥

```python
allow_origins=["*"],
allow_credentials=True,
```

**这两个组合是互斥的**：浏览器会拒绝任何带凭据的跨域请求。
当前无鉴权所以没暴露，一旦加上登录就会全线 401。
（与短剧项目 `app.py` 是同一个错误，属于同一个知识盲区，值得记一笔。）

### 5.2 `bm25_search.py` 的 0.85 阈值没有物理意义

```python
scores = self.bm25.get_scores(tokenized_query)
softmax_scores = self._softmax(scores)
if softmax_scores[best_idx] >= threshold:   # 0.85
```

`BM25Okapi` 分数无上界，softmax 之后的结果**依赖候选集大小**：
库里 10 条问题，best 很容易 > 0.85；库里 10 万条，best 几乎不可能 > 0.85。
**阈值必须随语料规模重新标定**，否则要么全命中、要么全不命中。

规则库场景应该改成：`top1 分数 >= 绝对阈值` + `top1 与 top2 的差值 >= 相对阈值`（双条件）。

### 5.3 `config.py` 里 `password` 用了 `getint`

```python
self.REDIS_PASSWORD = os.getenv('REDIS_PASSWORD',
                        self.config.getint('redis', 'password', fallback='123456'))
```

当前 `config.ini` 里密码是纯数字所以没炸。换成含字母的密码立即 `ValueError`。
应改 `get`。

### 5.4 源码里硬编码了 API Key

`config.py` 第 70-72 行与 `config.ini` 第 31 行都有明文 `sk-...`。
**拷贝时必须清掉**，改为纯环境变量。这类东西一旦进 git 历史，清理成本极高。

次要：`VALID_SOURCES = eval(...)` 对 env 值用了 `eval`，应改 `json.loads`。

---

## 6. 建议目录（copy 出来做新项目，不动原目录）

```
rules_rag/
├── app/
│   ├── main.py                  # 复用 app.py 骨架（修 CORS）
│   ├── api/{query,review,admin}.py
│   └── static/
├── core/
│   ├── config.py                # 复用 base/config.py
│   ├── logger.py                # 复用 base/logger.py
│   ├── vector_store.py          # 复用 + schema 扩展 + 懒加载 reranker
│   └── llm.py                   # 复用 new_main.call_dashscope
├── ingest/
│   ├── loaders/                 # 复用 edu_*loader（PDF/DOCX/OCR）
│   ├── clause_splitter.py       # 新写
│   ├── metadata.py              # 新写
│   └── change_pipeline.py       # 新写
├── retrieve/
│   ├── hybrid.py                # 复用 hybrid_search_with_rerank
│   ├── filters.py               # 新写
│   └── faq_fast.py              # 改 bm25_search（版本化缓存键 + 双阈值）
├── generate/
│   ├── prompts.py               # 重写
│   ├── risk_router.py           # 新写
│   └── citation_verifier.py     # 新写
├── eval/
│   ├── datasets/
│   ├── metrics.py               # 新写
│   └── run_regression.py        # 新写
├── data/{raw,process}/
└── models/                      # 软链或复制 rag_qa/models
```

**不建议在原目录改**：那目录是课程素材，改了会污染基线，也无法对比"改了什么"。
copy 一份到 `rules_rag/` 下，原目录只读保留。

---

## 7. 施工顺序（依赖关系决定，不能乱）

| 步 | 做什么 | 依赖 | 验证方式 |
|---|---|---|---|
| 1 | 复制骨架到 `rules_rag/`，清掉明文 key，修 CORS | — | 能起服务、能连 Milvus |
| 2 | 改 `vector_store` schema（加 6 个字段）+ 懒加载 reranker | 1 | 建空 collection 成功 |
| 3 | 写 `clause_splitter`，用 1 份真实规范验证 | 2 | 条款不跨块，路径正确 |
| 4 | 写 `filters.py` 多条件 expr | 2、3 | 时效/类目过滤生效 |
| 5 | 修 `_get_unique_parent_docs`，保留 metadata | 2 | 重排后仍能拿到条款号 |
| 6 | 重写 `prompts.py` + 写 `citation_verifier` | 5 | 生成的条款号 100% 在召回集内 |
| 7 | 写 `risk_router`（L1/L2/L3） | 6 | 高风险问题必转人工 |
| 8 | 写 `change_pipeline` + `run_regression` | 3–7 | 改一条规则能拦住 |

**第 3 步是全项目的分水岭**：条款切分不对，后面 5 步全是白做。
建议先只灌一份文档（《商家售后服务管理规范》），把 3→5 跑通，再批量。

---

## 8. 诚实性策略（重要）

这套代码基线来自课程素材（库名 `itcast08.edurag`，`config.ini` 里可见）。
**复用工程骨架完全合理**——就像用 FastAPI、用 LangChain。
但必须做到两点：

1. **README 写明基线来源**，并列出「自研部分」清单（§2 的 ✍️ 那一栏）
2. **能解释每一处改动的原因**，而不是只改了参数

面试官真正在意的不是"这段代码是不是你敲的"，是"你知不知道它为什么这么设计、
以及你的场景为什么不能照搬"。§3 那四条不兼容，就是最好的回答材料。
反过来，"我基于一个开源/课程 RAG 做了改造"比"我从零写的"更容易被追问穿，
前者至少诚实。

---

*文档版本：v1 · 2026-10-27 · 只读评估，未修改原目录任何文件*
*配套：`00-设计总纲.md`、`01-电商系统全流程拆解.md`*
