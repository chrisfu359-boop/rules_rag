"""公共配置与存储访问（MySQL / SQLite 双后端）。

为什么要有 SQLite 后备：
  MySQL / Redis / Milvus 都跑在本机 Docker Desktop 里。Docker daemon 退出后，
  WSL 被安全策略阻断，服务无法恢复（这不是我能通过改代码解决的问题）。
  为了不让"数据服务挂掉"变成"项目挂掉"，存储层做后端自动切换：
  启动时探测 MySQL，通则走 MySQL，不通则走 SQLite（表结构逐字段一致）。
  上层 pipeline / retrieval / app 的调用接口完全不变。

这个设计本身也是可讲的：规则库 353 条 + 订单 5000 条这种规模，
独立数据库进程的必要性很低；把存储做成可替换，比绑死一个 MySQL 更工程化。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

DB = dict(host=os.getenv("MYSQL_HOST", "127.0.0.1"),
          port=int(os.getenv("MYSQL_PORT", "3307")),
          user=os.getenv("MYSQL_USER", "root"),
          password=os.getenv("MYSQL_PASSWORD", "123456"),
          database="rules_rag", charset="utf8mb4", autocommit=True,
          # 容器里的 MySQL 默认 UTC，而业务时间必须和用户看到的本地时间一致。
          # 不设这个，所有 created_at 都会比本地时间少 8 小时，
          # 而且 DEFAULT CURRENT_TIMESTAMP 写入的值也跟着错，藏得很深。
          init_command="SET time_zone = '+08:00'")

SQLITE_PATH = Path(os.getenv("SQLITE_PATH")
                   or (Path(__file__).resolve().parent / "data" / "rules.db"))
_BACKEND: str | None = None
_local = threading.local()


def backend() -> str:
    """后端选择。

    踩过的坑：原设计是"先探测 MySQL，通则用 MySQL"。但 MySQL（Docker）中途恢复时，
    里面还是旧的 353 条法规数据，而 SQLite 里已经是 406 条（含平台规则）。
    两个后端数据不一致时"谁活着用谁"，会导致平台规则凭空消失，而且日志一切正常。
    这跟"缓存不带规则版本号"是同一类故障：状态有两个来源，就必须有唯一权威。
    所以改为：默认 SQLite，要 MySQL 必须显式设置环境变量 DB_BACKEND=mysql。
    """
    global _BACKEND
    if _BACKEND is None:
        want = os.getenv("DB_BACKEND", "sqlite").strip().lower()
        if want == "mysql":
            import pymysql
            try:
                c = pymysql.connect(**DB, connect_timeout=2)
                c.close()
                _BACKEND = "mysql"
            except Exception as e:
                # MySQL 优先但允许掉线降级：Docker 停掉时服务不该跟着挂。
                # 前提是两个后端数据一致（都由 build_store / build_sqlite 从
                # 同一个 clauses.json 生成），否则"谁活着用谁"就会悄悄换数据源。
                print("[db] MySQL 不可用，降级 SQLite:", str(e)[:80])
                _BACKEND = "sqlite"
        else:
            _BACKEND = "sqlite"
    return _BACKEND


def _sqlite_conn() -> sqlite3.Connection:
    """线程本地 SQLite 连接：一次建连、复用到线程结束。

    原来每条 SQL 都 new 一个连接 + 每次 commit 一次 fsync（synchronous=FULL），
    20 并发下 P95 能到 2 秒。改成 WAL + synchronous=NORMAL + 线程本地复用后：
    读不阻塞写、写不阻塞读，也不用每请求付建连和日志初始化的成本。
    """
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(SQLITE_PATH, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        _local.conn = c
    return c


def conn():
    if backend() == "sqlite":
        return _sqlite_conn()
    import pymysql
    return pymysql.connect(**DB)


def fetch_all(sql: str, args: tuple = ()) -> list[dict]:
    if backend() == "sqlite":
        c = _sqlite_conn()
        return [dict(r) for r in c.execute(sql.replace("%s", "?"), args).fetchall()]
    c = conn()
    try:
        with c.cursor(__import__("pymysql").cursors.DictCursor) as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        c.close()


def execute(sql: str, args: tuple = ()) -> None:
    """写操作。SQLite 下自动提交。"""
    if backend() == "sqlite":
        c = _sqlite_conn()
        c.execute(sql.replace("%s", "?"), args)
        c.commit()
        return
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
    finally:
        c.close()


def execute_many(sql: str, rows: list[tuple]) -> int:
    """批量写，返回条数。

    补数千条商品明细必须走批量：逐条 execute 在 SQLite 下每条都要开关连接、
    每次 commit 一次 fsync，5000 条能跑到几分钟，启动流程会像卡死；
    MySQL 下每条一次建连也慢一个数量级。批量提交是量级差别，不是微优化。
    """
    if not rows:
        return 0
    if backend() == "sqlite":
        c = _sqlite_conn()
        c.executemany(sql.replace("%s", "?"), rows)
        c.commit()
        return len(rows)
    c = conn()
    try:
        with c.cursor() as cur:
            cur.executemany(sql, rows)
    finally:
        c.close()
    return len(rows)


def insert(sql: str, args: tuple = ()) -> int:
    """写操作并返回自增主键。注册用户、建会话都要拿到新 id。"""
    if backend() == "sqlite":
        c = _sqlite_conn()
        cur = c.execute(sql.replace("%s", "?"), args)
        c.commit()
        return int(cur.lastrowid or 0)
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return int(cur.lastrowid or 0)
    finally:
        c.close()


def load_clauses() -> list[dict]:
    """只取现行有效（active）条款 —— 这就是"先过滤"在存储层的体现。"""
    return fetch_all("""
        SELECT c.id, c.clause_no, c.clause_num, c.order_index, c.clause_path, c.content,
               c.scene, c.risk_level, d.doc_name, d.source_url, d.effective_from,
               c.platform, v.status
        FROM t_clause c
        JOIN t_rule_doc d ON c.doc_id = d.id
        JOIN t_clause_version v ON v.clause_id = c.id AND v.status = 'active'
        ORDER BY c.doc_id, c.order_index, c.id
    """)


def find_order(query: str) -> dict | None:
    """从问题里抠订单号；抠不到就按语境挑一条样例订单。"""
    import re
    m = re.search(r"\d{12,20}", query)
    if m:
        rows = fetch_all("SELECT * FROM t_order WHERE order_no=%s", (m.group(0),))
        if rows:
            return rows[0]
    if "已发货" in query or "物流" in query:
        st = "已发货"
    elif "未发货" in query or "催发货" in query:
        st = "待发货"
    elif "退款" in query or "退货" in query or "售后" in query:
        st = "售后中"
    else:
        return None
    rows = fetch_all("SELECT * FROM t_order WHERE status=%s LIMIT 1", (st,))
    return rows[0] if rows else None
