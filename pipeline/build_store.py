"""建库、灌条款、生成模拟订单数据。

运行：python pipeline/build_store.py
依赖：MySQL 8（本机 3307），pymysql
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

import pymysql

DB = dict(host=os.getenv("MYSQL_HOST", "127.0.0.1"),
          port=int(os.getenv("MYSQL_PORT", "3307")),
          user=os.getenv("MYSQL_USER", "root"),
          password=os.getenv("MYSQL_PASSWORD", "123456"),
          charset="utf8mb4")

SCHEMA = """
CREATE TABLE IF NOT EXISTS t_rule_doc (
  id INT AUTO_INCREMENT PRIMARY KEY,
  doc_name VARCHAR(128) NOT NULL UNIQUE,
  doc_type VARCHAR(32) NOT NULL,
  platform VARCHAR(32) NOT NULL DEFAULT 'all',
  publish_date DATE NULL,
  effective_from DATE NULL,
  source_url VARCHAR(512) NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS t_clause (
  id INT AUTO_INCREMENT PRIMARY KEY,
  doc_id INT NOT NULL,
  clause_no VARCHAR(64) NOT NULL,
  clause_num INT NULL,
  order_index INT NOT NULL DEFAULT 0,
  clause_path VARCHAR(512) NOT NULL,
  chapter VARCHAR(128) NULL,
  section VARCHAR(128) NULL,
  content TEXT NOT NULL,
  scene VARCHAR(64) NULL,
  risk_level VARCHAR(8) NOT NULL DEFAULT 'L1',
  category VARCHAR(64) NOT NULL DEFAULT 'all',
  platform VARCHAR(32) NOT NULL DEFAULT 'all',
  content_hash CHAR(64) NOT NULL,
  UNIQUE KEY uk_doc_clause (doc_id, clause_no),
  KEY idx_scene (scene),
  KEY idx_risk (risk_level),
  FULLTEXT KEY ft_content (content) WITH PARSER ngram
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS t_clause_version (
  id INT AUTO_INCREMENT PRIMARY KEY,
  clause_id INT NOT NULL,
  version INT NOT NULL DEFAULT 1,
  publish_date DATE NULL,
  effective_from DATE NULL,
  effective_to DATE NULL,
  content_hash CHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_clause_ver (clause_id, version),
  KEY idx_status (clause_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS t_qa_log (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  session_id VARCHAR(64) NULL,
  query VARCHAR(512) NOT NULL,
  scene VARCHAR(64) NULL,
  risk_level VARCHAR(8) NULL,
  retrieved_clause_ids VARCHAR(512) NULL,
  cited_clauses VARCHAR(512) NULL,
  answer TEXT NULL,
  latency_ms INT NULL,
  cache_hit TINYINT DEFAULT 0,
  generator VARCHAR(64) NULL,
  llm_ms DECIMAL(10,1) NULL,
  token_cost DECIMAL(10,6) DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS t_order (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  order_no VARCHAR(32) NOT NULL UNIQUE,
  shop_name VARCHAR(64) NOT NULL,
  category VARCHAR(32) NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  status VARCHAR(16) NOT NULL,
  paid_at DATETIME NULL,
  shipped_at DATETIME NULL,
  received_at DATETIME NULL,
  aftersale_type VARCHAR(32) NULL,
  aftersale_applied_at DATETIME NULL,
  remarks VARCHAR(255) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ---- 条款打标规则（先用词表，后续可换模型，见 docs/design/04 §1 ②）

RISK_RULES = [
    ("L3", ["罚款", "处罚", "吊销", "刑事责任", "责令停业", "没收", "记入信用档案", "连带责任"]),
    ("L2", ["日内", "工作日", "期限", "退还", "赔偿", "届满", "之日起"]),
]

SCENE_RULES = [
    ("aftersale.return", ["退货", "退款", "仅退款", "七日", "无理由", "换货", "补寄", "维修",
                          "售后", "商品完好", "寄回", "签收"]),
    ("logistics.delivery", ["交付", "快递", "物流", "运输", "签收", "发货", "揽收"]),
    ("marketing.ad", ["广告", "宣传", "极限", "国家级", "最高级", "最佳", "虚假", "误导",
                      "可识别性", "竞价排名"]),
    ("price.label", ["价格", "标价", "明码", "收费", "费用", "押金", "保证金"]),
    ("privacy.data", ["个人信息", "收集", "使用信息", "隐私", "保密"]),
    ("platform.onboarding", ["平台内经营者", "入驻", "登记", "资质", "行政许可", "核验"]),
    ("governance.penalty", ["监督管理", "违法", "处置", "报告", "检查", "公示", "扣分",
                            "违规", "处罚", "违约金", "划扣"]),
]

# 老库兼容：clause_num 原为 NOT NULL（存不下 2.3.1），补 order_index 供平台规则排序
MIGRATIONS = [
    "ALTER TABLE t_clause MODIFY clause_num INT NULL",
    "ALTER TABLE t_clause ADD COLUMN order_index INT NOT NULL DEFAULT 0",
]


def tag(content: str) -> tuple[str, str | None]:
    risk = "L1"
    for lv, kws in RISK_RULES:
        if any(k in content for k in kws):
            risk = lv
            break
    scene = None
    best = 0
    for sc, kws in SCENE_RULES:
        hit = sum(1 for k in kws if k in content)
        if hit > best:
            best, scene = hit, sc
    return risk, scene


def gen_orders(cur, n: int = 5000, seed: int = 20261029) -> None:
    random.seed(seed)
    shops = [("优选生活旗舰店", "家居"), ("数码前线专营店", "3C"), ("山野生鲜直营", "生鲜"),
             ("潮流服饰工厂店", "服饰"), ("母婴安心馆", "母婴"), ("美妆小铺", "美妆"),
             ("运动装备仓", "运动"), ("书屋文创", "图书")]
    statuses = ["待付款", "待发货", "已发货", "已完成", "已关闭", "售后中"]
    aftersales = [None, None, None, "仅退款", "退货退款", "换货", "补寄"]
    rows = []
    base = datetime(2026, 9, 1, 9, 0, 0)
    for i in range(n):
        shop, cat = shops[i % len(shops)]
        paid = base + timedelta(minutes=random.randint(0, 55 * 24 * 60))
        st = random.choices(statuses, weights=[3, 12, 20, 55, 5, 5])[0]
        shipped = paid + timedelta(hours=random.randint(2, 48)) if st in ("已发货", "已完成", "售后中") else None
        received = (shipped + timedelta(days=random.randint(1, 5))) if st == "已完成" and shipped else None
        at = random.choice(aftersales) if st == "售后中" else None
        applied = (shipped + timedelta(hours=random.randint(1, 72))) if at and shipped else None
        rows.append((f"2026{paid:%m%d}{i:06d}", shop, cat,
                     round(random.uniform(19, 2999), 2), st, paid, shipped, received,
                     at, applied, None))
    cur.executemany("""INSERT IGNORE INTO t_order
        (order_no, shop_name, category, amount, status, paid_at, shipped_at,
         received_at, aftersale_type, aftersale_applied_at, remarks)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    clauses = json.loads((root / "data" / "processed" / "clauses.json").read_text(encoding="utf-8"))

    conn = pymysql.connect(**DB, autocommit=False)
    cur = conn.cursor()
    cur.execute("CREATE DATABASE IF NOT EXISTS rules_rag DEFAULT CHARSET utf8mb4")
    cur.execute("USE rules_rag")
    for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
        cur.execute(stmt)
    for m in MIGRATIONS:                     # 老库升级，已改过则忽略
        try:
            cur.execute(m)
        except Exception:
            pass

    # 文档
    docs: dict[str, int] = {}
    for c in clauses:
        name = c["doc_name"]
        if name not in docs:
            cur.execute("""INSERT INTO t_rule_doc (doc_name, doc_type, platform, publish_date,
                           effective_from, source_url) VALUES (%s,%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)""",
                        (name, c.get("doc_type", "法律"), c.get("platform", "all"),
                         c.get("publish_date"), c.get("effective_from"), c.get("source_url", "")))
            docs[name] = cur.lastrowid

    # 条款 + 版本
    inserted = 0
    for c in clauses:
        risk, scene = tag(c["content"])
        h = hashlib.sha256(c["content"].encode()).hexdigest()
        cur.execute("""INSERT INTO t_clause (doc_id, clause_no, clause_num, order_index, clause_path,
                       chapter, section, content, scene, risk_level, category, platform, content_hash)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE content=VALUES(content), scene=VALUES(scene),
                       risk_level=VALUES(risk_level), order_index=VALUES(order_index),
                       content_hash=VALUES(content_hash)""",
                    (docs[c["doc_name"]], c["clause_no"], c.get("clause_num"),
                     c.get("order_index", 0), c["clause_path"],
                     c.get("chapter"), c.get("section"), c["content"], scene, risk,
                     c.get("category", "all"), c.get("platform", "all"), h))
        cid = cur.lastrowid or cur.execute(
            "SELECT id FROM t_clause WHERE doc_id=%s AND clause_no=%s",
            (docs[c["doc_name"]], c["clause_no"])) or cur.fetchone()[0]
        cur.execute("""INSERT INTO t_clause_version (clause_id, version, publish_date,
                       effective_from, effective_to, content_hash, status)
                       VALUES (%s,1,%s,%s,NULL,%s,'active')
                       ON DUPLICATE KEY UPDATE content_hash=VALUES(content_hash)""",
                    (cid, c.get("publish_date"), c.get("effective_from"), h))
        inserted += 1

    gen_orders(cur)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM t_clause")
    n_clause = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM t_order")
    n_order = cur.fetchone()[0]
    cur.execute("SELECT risk_level, COUNT(*) FROM t_clause GROUP BY risk_level ORDER BY risk_level")
    risk_dist = cur.fetchall()
    cur.execute("SELECT doc_name, COUNT(*) FROM t_clause c JOIN t_rule_doc d ON c.doc_id=d.id GROUP BY doc_name")
    per_doc = cur.fetchall()
    conn.close()

    print(f"条款入库 {inserted} 条（库内 {n_clause}）")
    print(f"订单生成 {n_order} 条")
    print("风险分布:", risk_dist)
    print("按文档:", per_doc)


if __name__ == "__main__":
    main()
