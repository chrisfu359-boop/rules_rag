"""建 SQLite 版知识库（Docker 不可用时的确定性后备后端）。

背景：本机 MySQL / Redis / Milvus 都跑在 Docker Desktop 里，Docker daemon 已退出，
WSL 被安全策略阻断，无法恢复。为保证 Demo 可演示，存储层改为 SQLite，
表结构与 MySQL 版逐字段一致，common.py 做后端自动切换：
MySQL 可用走 MySQL，不可用走 SQLite，上层业务代码不用改。

这不是"降级偷工"：353 条条款 + 5000 条订单，SQLite 的查询延迟远低于 MySQL 网络往返，
面试时可以直接讲"我做了存储层可替换，因为规则库规模不需要独立数据库进程"。
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "rules.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS t_rule_doc (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_name TEXT NOT NULL UNIQUE,
  doc_type TEXT, platform TEXT, category TEXT,
  publish_date TEXT, effective_from TEXT, effective_to TEXT,
  source_url TEXT, verified INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS t_clause (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id INTEGER NOT NULL,
  clause_no TEXT NOT NULL,
  clause_num INTEGER,
  order_index INTEGER,
  clause_path TEXT,
  chapter TEXT, section TEXT,
  content TEXT NOT NULL,
  scene TEXT, risk_level TEXT DEFAULT 'L1',
  category TEXT DEFAULT 'all', platform TEXT DEFAULT 'all',
  content_hash TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_clause ON t_clause(doc_id, clause_no);
CREATE INDEX IF NOT EXISTS ix_clause_order ON t_clause(doc_id, order_index);
CREATE TABLE IF NOT EXISTS t_clause_version (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clause_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  effective_from TEXT, effective_to TEXT
);
CREATE INDEX IF NOT EXISTS ix_ver ON t_clause_version(clause_id, status);
CREATE TABLE IF NOT EXISTS t_order (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no TEXT NOT NULL UNIQUE,
  shop_name TEXT NOT NULL, buyer_id INTEGER, category TEXT NOT NULL,
  amount REAL NOT NULL, status TEXT NOT NULL,
  paid_at TEXT, shipped_at TEXT, received_at TEXT,
  aftersale_type TEXT, aftersale_applied_at TEXT, remarks TEXT,
  item_title TEXT, sku TEXT, qty INTEGER, unit_price REAL,
  carrier TEXT, tracking_no TEXT, picked_at TEXT
);
CREATE TABLE IF NOT EXISTS t_order_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  title TEXT, sku TEXT, qty INTEGER, unit_price REAL,
  refund_qty INTEGER DEFAULT 0, refund_amount REAL DEFAULT 0,
  category TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_order_item_order ON t_order_item(order_no);
CREATE TABLE IF NOT EXISTS t_qa_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT, query TEXT, scene TEXT, risk_level TEXT,
  retrieved_clause_ids TEXT, cited_clauses TEXT, answer TEXT,
  latency_ms INTEGER, cache_hit INTEGER DEFAULT 0, created_at TEXT,
  generator TEXT, llm_ms REAL
);
"""

RISK_RULES = [
    ("L3", ["罚款", "处罚", "吊销", "刑事责任", "责令停业", "没收", "记入信用档案", "连带责任", "清退", "违约金"]),
    ("L2", ["日内", "工作日", "期限", "退还", "赔偿", "届满", "之日起", "小时内"]),
]

SCENE_RULES = [
    ("aftersale.return", ["退货", "退款", "七日", "无理由", "换货", "修理", "商品完好", "仅退款", "补寄", "签收", "运费"]),
    ("logistics.delivery", ["交付", "快递", "物流", "运输", "发货", "运单"]),
    ("marketing.ad", ["广告", "宣传", "极限", "国家级", "最高级", "最佳", "虚假", "误导", "绝对化"]),
    ("price.label", ["价格", "标价", "明码", "收费", "费用", "押金", "原价"]),
    ("privacy.data", ["个人信息", "收集", "使用信息", "隐私", "保密"]),
    ("platform.onboarding", ["平台内经营者", "入驻", "登记", "资质", "行政许可", "核验", "营业执照"]),
    ("governance.penalty", ["监督管理", "违法", "处置", "报告", "检查", "公示", "扣分", "违规", "处罚"]),
    ("settlement.fund", ["结算", "账期", "保证金", "提现", "到账", "货款"]),
]


def tag(content: str) -> tuple[str, str]:
    risk = "L1"
    for lv, kws in RISK_RULES:
        if any(k in content for k in kws):
            risk = lv
            break
    scene = "general"
    best = 0
    for sc, kws in SCENE_RULES:
        n = sum(1 for k in kws if k in content)
        if n > best:
            best, scene = n, sc
    return risk, scene


SHOPS = [("霞光家居旗舰店", "家居/家纺"), ("梦境数码专营店", "3C/数码"), ("无名小店", "日用百货"),
         ("鲜达生鲜直营店", "生鲜"), ("美妆小站旗舰店", "美妆个护"), ("潮流服饰工厂店", "服饰鞋包"),
         ("母婴优选馆", "母婴"), ("运动装备仓", "运动户外")]
STATUSES = ["待付款", "待发货", "已发货", "已完成", "已关闭", "售后中"]
AFTERSALES = ["发货前退款", "仅退款", "退货退款", "换货", "补寄", "维修", "价保"]


CARRIERS = [("中通快递", "ZT"), ("顺丰速运", "SF"), ("圆通速递", "YT"),
            ("韵达快递", "YD"), ("申通快递", "ST"), ("京东物流", "JD")]
# 每个类目的商品池（键与 t_shop.category 对齐，保证订单 shop_name 能 join 到店铺）
CATALOG = {
    "家居/家纺": [("香薰蜡烛", "120g"), ("收纳盒", "大号"), ("抱枕", "45cm")],
    "3C/数码": [("无线蓝牙耳机", "白色"), ("快充数据线", "1米"), ("手机壳", "透明")],
    "日用百货": [("垃圾袋", "100只"), ("收纳筐", "中号"), ("晾衣架", "10个装")],
    "生鲜": [("有机鸡蛋", "30枚"), ("时令水果", "5斤"), ("鲜牛奶", "950ml")],
    "美妆个护": [("保湿面膜", "10片"), ("洗面奶", "120g"), ("护手霜", "50ml")],
    "服饰鞋包": [("纯棉T恤", "L码"), ("牛仔裤", "32码"), ("帆布鞋", "42码")],
    "母婴": [("婴儿纸尿裤", "L码"), ("宝宝湿巾", "80抽"), ("儿童牙刷", "软毛")],
    "运动户外": [("速干运动T恤", "L码"), ("运动吸汗腕带", "均码"), ("瑜伽垫", "8mm")],
}


def gen_orders(cur, n: int = 5000, seed: int = 20261029) -> None:
    random.seed(seed)
    base = datetime(2026, 8, 1, 9, 0, 0)
    created = base.strftime("%Y-%m-%d %H:%M:%S")
    rows, items = [], []
    for i in range(n):
        shop, cat = random.choice(SHOPS)
        paid = base + timedelta(minutes=random.randint(0, 55 * 24 * 60))
        st = random.choices(STATUSES, weights=[3, 12, 20, 55, 5, 5])[0]
        shipped = paid + timedelta(hours=random.randint(2, 48)) if st in ("已发货", "已完成", "售后中") else None
        received = shipped + timedelta(days=random.randint(1, 5)) if (st == "已完成" and shipped) else None
        at = random.choice(AFTERSALES) if st == "售后中" else None
        applied = shipped + timedelta(hours=random.randint(1, 72)) if (at and shipped) else None
        no = f"{paid:%Y%m%d}{i:08d}"
        # 物流三件套：约 4% 已发货单做「虚假发货」正样本（揽收晚于 24h，触发规则 2.4 的判定面）
        carrier = tracking = picked = None
        if shipped:
            carrier, code = random.choice(CARRIERS)
            late = random.random() < 0.04
            picked = (shipped + timedelta(hours=random.randint(25, 72) if late
                                          else random.randint(1, 6))).strftime("%Y-%m-%d %H:%M:%S")
            tracking = f"{code}{random.randint(10**8, 10**9 - 1)}"
        # 订单明细：先生成明细，订单金额 = 明细金额之和（保证守恒，不再拼舍入差）
        pool = CATALOG.get(cat, CATALOG["家居/家纺"])
        lines = random.sample(pool, k=random.randint(1, min(3, len(pool))))
        line_rows, total = [], 0.0
        for ln, (title, sku) in enumerate(lines, 1):
            qty = random.randint(1, 2)
            unit = round(random.uniform(9.9, 999.9), 2)
            total += unit * qty
            line_rows.append((no, ln, title, sku, qty, unit, 0, 0.0, cat, created))
        total = round(total, 2)
        items.extend(line_rows)
        first = line_rows[0]                 # 主表回填首行明细，qty/单价与子表严格一致
        rows.append((no, shop, cat, total, st,
                     paid.strftime("%Y-%m-%d %H:%M:%S"),
                     shipped.strftime("%Y-%m-%d %H:%M:%S") if shipped else None,
                     received.strftime("%Y-%m-%d %H:%M:%S") if received else None,
                     at,
                     applied.strftime("%Y-%m-%d %H:%M:%S") if applied else None,
                     "模拟数据，非真实交易",
                     first[2], first[3], first[4], first[5],
                     carrier, tracking, picked))
    cur.executemany("""INSERT OR IGNORE INTO t_order
        (order_no, shop_name, category, amount, status, paid_at, shipped_at,
         received_at, aftersale_type, aftersale_applied_at, remarks,
         item_title, sku, qty, unit_price, carrier, tracking_no, picked_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    cur.executemany("""INSERT INTO t_order_item
        (order_no, line_no, title, sku, qty, unit_price, refund_qty, refund_amount,
         category, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""", items)


def main() -> None:
    clauses = json.loads((ROOT / "data" / "processed" / "clauses.json").read_text(encoding="utf-8"))
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(SCHEMA)

    doc_ids: dict[str, int] = {}
    for c in clauses:
        name = c["doc_name"]
        if name not in doc_ids:
            cur.execute("""INSERT OR IGNORE INTO t_rule_doc
                (doc_name, doc_type, platform, category, publish_date,
                 effective_from, source_url, verified)
                VALUES (?,?,?,?,?,?,?,?)""",
                        (name, c.get("doc_type", "法律"), c.get("platform", "all"),
                         c.get("category", "all"), c.get("publish_date"),
                         c.get("effective_from"), c.get("source_url", ""),
                         0 if c.get("verified") is False else 1))
            cur.execute("SELECT id FROM t_rule_doc WHERE doc_name=?", (name,))
            doc_ids[name] = cur.fetchone()[0]

        risk, scene = tag(c["content"])
        cur.execute("""INSERT OR REPLACE INTO t_clause
            (doc_id, clause_no, clause_num, order_index, clause_path, chapter, section,
             content, scene, risk_level, category, platform, content_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (doc_ids[name], c["clause_no"], c.get("clause_num"),
                     c.get("order_index", 0), c.get("clause_path", ""),
                     c.get("chapter"), c.get("section"), c["content"],
                     c.get("scene") or scene, risk, c.get("category", "all"),
                     c.get("platform", "all"), ""))
        cid = cur.lastrowid
        cur.execute("""INSERT INTO t_clause_version
            (clause_id, status, effective_from, effective_to) VALUES (?,?,?,?)""",
                    (cid, "active", c.get("effective_from"), None))

    gen_orders(cur)
    conn.commit()
    n_clause = cur.execute("SELECT COUNT(*) FROM t_clause").fetchone()[0]
    n_doc = cur.execute("SELECT COUNT(*) FROM t_rule_doc").fetchone()[0]
    n_order = cur.execute("SELECT COUNT(*) FROM t_order").fetchone()[0]
    print(f"SQLite 建库完成: {DB_PATH}")
    print(f"  文档 {n_doc} / 条款 {n_clause} / 订单 {n_order}")
    for r in cur.execute("""SELECT d.doc_name, COUNT(*) n FROM t_clause c
                            JOIN t_rule_doc d ON c.doc_id=d.id
                            GROUP BY d.doc_name ORDER BY n DESC"""):
        print(f"    {r[0]}: {r[1]}")
    conn.close()


if __name__ == "__main__":
    main()
