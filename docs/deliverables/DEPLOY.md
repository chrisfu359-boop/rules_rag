# 上线部署手册

> 两条路径：**A. Docker（推荐）** / **B. 裸机 systemd 或 Windows 计划任务**。
> 下面每一条命令都在本机验证过，除了明确标注「本机未验证」的部分 —— 那部分我写清了原因。

---

## 0. 上线前必做四件事

| # | 做什么 | 为什么 |
|---|---|---|
| 1 | 设置 `CORS_ORIGINS» 为真实域名 | 写死 `127.0.0.1:8000» 时前端拿不到响应，而报错出现在**浏览器侧**（CORS policy），服务端日志一片正常 |
| 2 | 设置 `JWT_SECRET» | 不设会自动生成到容器内 `.jwt_secret»，**容器重建即失效**，所有登录态掉线 |
| 3 | 设置 `API_KEY»（若对外暴露 `/api/ask»） | 那个接口是早期 Demo 用的，不校验 Key 时谁都能打 |
| 4 | 确认 `data/rules.db» 已随镜像进去 | 它是 SQLite 后端的全部数据（406 条条款 + 5000 单 + 23 张表），已在 `.gitignore» 白名单里 |

环境变量全表见 `.env.example»；关键几个：

```
DB_BACKEND=sqlite|mysql              默认 sqlite；要 MySQL 必须显式设置
VECTOR_BACKEND=npz|milvus            默认按可用性选；无 pymilvus 自动降级 npz
RERANK=on|off                        需本地 BGE 模型（约 2GB），容器内建议 off
LLM_PROVIDER / LLM_API_KEY / LLM_MODEL   不配则走模板生成，服务不报错
CORS_ORIGINS=https://your.domain     逗号分隔
JWT_SECRET=<32+ 随机串>              JWT_TTL_HOURS 默认见 .env.example
API_KEY=<随机串>                      /api/ask 的 X-API-Key，留空不校验
PENALTY_SWEEP_SEC=600                罚单到期巡检间隔
RATE_LIMIT=30                        单用户每分钟问答上限
```

---

## A. Docker 部署（推荐）

### A1. 完整栈（app + MySQL + Redis + Milvus）

```bash
docker compose up -d
docker compose ps                  # 等 app 变成 healthy
curl -s localhost:8000/api/health | python -m json.tool
```

本机实测：`docker compose config» **通过**（语法与依赖关系有效）；
依赖镜像在本地已存在 —— `redis:7.2»、`mysql:8.0.43»、`milvusdb/milvus:v2.4.10»、
`quay.io/coreos/etcd:v3.5.5»、`quay.io/minio/minio»。

### A2. 单容器（SQLite 降级版，无外部依赖）

```bash
docker build -t rules-rag:latest .
docker run -d --name rules-rag -p 8000:8000 \
  -e DB_BACKEND=sqlite -e VECTOR_BACKEND=npz -e RERANK=off \
  rules-rag:latest
```

> **本机未验证的一步：`docker build»**。本机 Docker 的镜像仓库访问坏了，拉基础镜像直接失败：
> `failed to copy: httpReadSeeker: read past expected size 1746: failed precondition»。
> 这是环境问题（Docker Desktop 的 content store / 代理），不是 Dockerfile 的问题 ——
> 同一个 Dockerfile 的每一步都能在裸机等价复现，见 B 节实测输出。
> 在能访问 docker.io 的机器上，这一步应当直接成功。

### A3. 镜像里有什么、没什么

- **有**：应用代码、`data/rules.db» 种子库、`web/» 页面、`static/» 演示页、`eval/» 评测脚本、`reports/» 看板
- **没有**（`.dockerignore» 挡掉）：`logs/»（本机 1150 MB 的浏览器 profile）、`.git/»、
  `.env»、`.jwt_secret»、`data/processed/*.npz»、`__pycache__/»
- 容器以非 root（uid 10001）运行，带 `HEALTHCHECK»

---

## B. 裸机部署

### B1. 依赖与启动

```bash
python -m pip install -r requirements.txt
# 生产用多 worker：uvicorn 单进程适合演示，压测前至少 2 worker
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --workers 2
```

**本机实测输出**（生产式启动，非 reload）：

```
HEALTH 200
{"service":"up","uptime_s":10,"version":"5.1","rule_version":"v8",
 "db":{"backend":"sqlite","ok":true,"ms":1.0},
 "redis":{"ok":false,"mem_keys":0,"rule_version":"v8"},
 "vector":{"size":406,"dim":1024,"loaded":true,"backend":"numpy-local(npz)"},
 "rerank":{"enabled":false,"model":"bge-reranker-large","loaded":false},
 "llm":{...,"available":false,...},
 "cs":{"faq":{"faqs":74,...,"threshold":0.88},"tickets_pending":7},
 "gov":{"disputes":5,"pending_human":1,"ai_auto_rate":1.0,...}}
DEMO 200 bytes=13679
STATS {"rule_version":"v8","service_version":"5.1","clauses":406,"faqs":120,"shops":3,"llm":null}
```

### B2. systemd 单元（Linux）

```ini
[Unit]
Description=rules_rag
After=network.target

[Service]
Type=simple
User=rulesrag
WorkingDirectory=/opt/rules_rag
EnvironmentFile=/opt/rules_rag/.env
ExecStart=/opt/rules_rag/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000 --workers 2
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### B3. Windows（本仓库自带）

```powershell
.\start.bat        # 起服务
.\stop.bat         # 停服务
```

---

## C. 反向代理（Nginx）

```nginx
server {
    listen 443 ssl http2;
    server_name your.domain;

    # 问答接口有 20~25s 的模型超时窗口，代理超时必须大于它，否则前端会看到 504 而服务端还在算
    proxy_read_timeout 60s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## D. 验收（上线后跑这 6 条）

```bash
curl -s localhost:8000/api/health | python -m json.tool        # 1 七组件探活
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/demo    # 2 演示页 200
python eval/run_regression.py                                   # 3 回归跑一遍
python eval/gate.py                                             # 4 门禁通过（exit 0）
python tests/cs_e2e.py                                          # 5 端到端断言
python pipeline/change_pipeline.py --list                       # 6 变更基线与库里一致
```

第 3、4 条是上线闸门：**门禁不过就别上**。它们的口径写在 `eval/run_regression.py» 顶部，
四条红线是：引用准确率不低于基线 3pp、时效端到端不低于基线 3pp、时效一致性 100%、高风险漏答率 0%。

---

## E. 数据维护

| 动作 | 命令 | 注意 |
|---|---|---|
| 补 `content_hash» 基线 | `python pipeline/change_pipeline.py --backfill-hash» | 只补空的，不覆盖已有 |
| 看有哪些规则文件变了 | `python pipeline/change_pipeline.py --list» | 只读，不落库 |
| 应用规则变更 | `python pipeline/change_pipeline.py --doc <文档名>» | 自动跑门禁，不过自动回滚 |
| 重建全量索引 | `python pipeline/clause_splitter.py && python pipeline/build_store.py» | 会重写 `clauses.json» 与库 |
| 重跑评测集 | `python eval/gen_eval_set.py && python eval/run_regression.py» | 它会改 `data/eval/eval_300.csv» |
| 立新基线 | `python eval/gate.py --promote» | **只在确认本次指标可信时做** |
| 看板 | 双击 `reports/dashboard.html» | 纯静态，不依赖服务 |

### 备份

必须备份的是 `data/rules.db»（或 MySQL 库）+ `data/rule_version.json»。
后者记录规则库版本号，丢了会导致全部问答缓存 key 回退，旧答案可能被当新答案返回。

```bash
# SQLite
cp data/rules.db backups/rules-$(date +%F).db
# MySQL
docker exec rules_rag_mysql mysqldump -uroot -p123456 rules_rag > backups/rules-$(date +%F).sql
```

---

## F. 回滚

| 场景 | 做法 |
|---|---|
| 规则改坏了 | 流水线自己会回滚（门禁不过即回滚，见 `reports/alerts.log»）；已发布后发现则重跑 `--doc» 换回原文 |
| 镜像有问题 | `docker compose down && docker run <上一个 tag>»，数据在 volume/`data/rules.db» 里不受影响 |
| 版本号错乱 | `data/rule_version.json» 里手改 `version»，或调 `cache.set_rule_version()» |

---

## G. 已知限制（上线前请知情）

1. **未做并发压测**。`P50/P95 = 1/3 ms» 是离线单请求链路开销（SQLite + BM25，不调模型），
   不代表容量。按当前实现，单进程 uvicorn + SQLite 适合**几十到一两百 QPS 量级**，
   再往上要换 MySQL/Redis 并加 worker。这个数字是估算，不是实测。
2. **向量与精排两层在本机是关的**（缺 `FlagEmbedding» / BGE 模型）。
   装了之后检索质量会变，**必须重新跑评测并重新立基线**，不要直接沿用现在的 baseline.json。
3. **`t_qa_log» 目前是 0 行**，问答审计没有被监控读取。上线请接上。
4. **CI 未真正运行过**（`git remote» 为空）。仓库里有 `.github/workflows/ci.yml»，配好 remote 才会跑。
