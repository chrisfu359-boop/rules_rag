"""补演示数据：10 条纠纷 + 5 笔近期订单（买家李澈）。

目的只有一个：汇报演示时，纠纷中心/我的订单/平台监管三个页面都有足够样本，
不用现场造数。数据写进 SQLite，再同步到 MySQL（两边保持一致）。

用法：
    python tools/seed_demo_extra.py
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "rules.db"
random.seed(20261101)


DISPUTES = [
    # (dtype, title, claim, claim_type, amount, status, liability, points, basis)
    ("shipping", "下单五天仍未发货，催促后客服失联",
     "付款五天了还是待发货，卖家不回消息，要求按规则处理并发货", "refund", 459.0,
     "pending_human", "merchant", 0.5, "《商家违规行为管理规则》2.4"),
    ("shipping", "快递显示已揽收但两天无物流更新",
     "物流一直停在已揽收，怀疑虚假发货，要求核实", "refund", 129.0,
     "pending", "undetermined", 0.0, ""),
    ("fraud", "客服引导加微信私下转账下单",
     "卖家让我加微信转账，说能便宜 200，付款后不发货", "refund", 899.0,
     "pending_human", "merchant", 48.0, "《商家违规行为管理规则》3.2"),
    ("safety", "充电宝充电时发烫冒烟",
     "商品使用中出现冒烟，存在安全隐患，要求下架并处理", "refund_compensate", 199.0,
     "pending_human", "merchant", 48.0, "《商家违规行为管理规则》3.3"),
    ("quality", "洗衣机用了三次就出现脱水异响",
     "脱水时噪音很大且有异响，要求换货", "exchange", 1899.0,
     "resolved", "merchant", 0.0, "《商家违规行为管理规则》4.1"),
    ("description", "页面写纯棉实际成分含大量化纤",
     "详情页标注 100% 纯棉，收到标签是 35% 棉，要求退一赔三", "refund_compensate", 268.0,
     "resolved", "merchant", 2.0, "《商家违规行为管理规则》2.2"),
    ("aftersale", "退货已签收一周仍未退款",
     "退货物流显示卖家已签收，退款却一直不到账", "refund", 356.0,
     "resolved", "merchant", 1.0, "《商家违规行为管理规则》2.3"),
    ("counterfeit", "口红批号查不到，疑似非正品",
     "扫码查不到批号，专柜比对色差明显，要求鉴定并处理", "refund_compensate", 329.0,
     "pending_human", "merchant", 24.0, "《商家违规行为管理规则》3.1"),
    ("service", "售后多次被挂断电话、态度恶劣",
     "联系售后被挂断三次，要求平台介入", "none", 0.0,
     "resolved", "merchant", 0.0, ""),
    ("other", "商家擅自取消已付款订单",
     "付款后第二天订单被单方面取消，理由写的是缺货", "refund", 799.0,
     "pending_human", "merchant", 0.0, "《商家违规行为管理规则》2.1"),
]

NEW_ORDERS = [
    ("待发货", "无线降噪耳机", "深空灰 / 标准版", 1, 599.0, 599.0, "蓝牙耳机", 1, ""),
    ("已发货", "实木书桌", "1.4m 原木色", 1, 1299.0, 1299.0, "家具", 1, "顺丰速运"),
    ("已完成", "有机纯牛奶 250ml×24", "整箱", 2, 69.0, 138.0, "食品生鲜", 9, "中通快递"),
    ("售后中", "瑜伽垫加厚防滑", "6mm 紫色", 1, 89.0, 89.0, "运动户外", 3, "圆通速递"),
    ("待付款", "机械键盘 87 键", "青轴 / 白色", 1, 349.0, 349.0, "电脑外设", 0, ""),
]

SHOPS = ["霞光家居旗舰店", "梦境数码专营店", "鲜达生鲜直营店", "运动装备仓",
         "潮流服饰工厂店", "母婴优选馆", "美妆小站旗舰店"]


def now_iso(offset_hours: float = 0) -> str:
    return (datetime.now() + timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    # 1) 新订单（李澈 buyer_id=5）
    orders = c.execute(
        "SELECT order_no, shop_name FROM t_order WHERE buyer_id=5 LIMIT 200").fetchall()
    order_pool = [(r["order_no"], r["shop_name"]) for r in orders] or []
    new_order_nos = []
    base_no = 20261101000000
    for i, (status, title, sku, qty, price, amount, cat, days_ago, carrier) in enumerate(NEW_ORDERS):
        ono = f"SO{base_no + i}"
        paid = now_iso(-days_ago * 24 - 3) if status != "待付款" else None
        shipped = now_iso(-days_ago * 24) if status in ("已发货", "已完成", "售后中") else None
        received = now_iso(-days_ago * 12) if status == "已完成" else None
        tracking = f"SF{random.randint(10**11, 10**12 - 1)}" if shipped else ""
        c.execute(
            "INSERT INTO t_order (order_no, shop_name, category, amount, status, paid_at, "
            "shipped_at, received_at, item_title, sku, qty, unit_price, carrier, tracking_no, buyer_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,5)",
            (ono, SHOPS[i % len(SHOPS)], cat, amount, status, paid, shipped, received,
             title, sku, qty, price, carrier, tracking))
        c.execute(
            "INSERT INTO t_order_item (order_no, line_no, title, sku, qty, unit_price, "
            "refund_qty, refund_amount, category, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ono, 1, title, sku, qty, price, 0, 0, cat, now_iso(-days_ago * 24 - 3)))
        new_order_nos.append(ono)
        order_pool.append((ono, SHOPS[i % len(SHOPS)]))

    # 2) 新纠纷（挂在真实订单上，买家李澈）
    seq = 20
    added = 0
    for j, (dtype, title, claim, ctype, amount, status, liab, pts, basis) in enumerate(DISPUTES):
        ono, shop_name = order_pool[(j * 7) % len(order_pool)]
        shop = c.execute("SELECT id FROM t_shop WHERE name=?", (shop_name,)).fetchone()
        if not shop and shop_name not in [s["name"] for s in c.execute("SELECT name FROM t_shop")]:
            shop = c.execute("SELECT id FROM t_shop LIMIT 1").fetchone()
        shop_id = shop["id"] if shop else 1
        dno = f"DP20261101{seq:04d}"
        seq += 1
        ts = now_iso(-(j + 1) * 5)
        verdict = (
            f"经核实，{title}。依据{basis or '平台规则'}，{'扣 ' + str(pts) + ' 分' if pts else '责令整改'}。"
            if status == "resolved" else "")
        c.execute(
            "INSERT INTO t_dispute (dispute_no, session_id, order_no, shop_id, buyer_id, buyer_name, "
            "dtype, title, claim, claim_type, claim_amount, evidence, status, stage, liability, "
            "ai_confidence, ai_remedy, ai_amount, ai_penalty, ai_points, ai_reasoning, ai_basis, "
            "verdict, verdict_by, verdict_at, priority, created_at, updated_at, closed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dno, "", ono, shop_id, 5, "李澈", dtype, title, claim, ctype, amount,
             json.dumps([{"k": "聊天记录", "v": "买家与客服对话截图"},
                         {"k": "订单截图", "v": ono}], ensure_ascii=False),
             status, "ai" if status == "resolved" else "human", liab,
             0.82 if status == "resolved" else round(random.uniform(0.45, 0.79), 2),
             "refund_compensate" if amount else "none", amount, "points" if pts else "none",
             pts, verdict or "AI 已给出定责建议，等待人工复核", basis,
             verdict, "platform" if status == "resolved" else None,
             ts if status == "resolved" else None,
             5, ts, ts, ts if status == "resolved" else None))
        added += 1

    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("PRAGMA journal_mode=DELETE")
    total_d = c.execute("SELECT COUNT(*) FROM t_dispute").fetchone()[0]
    total_o = c.execute("SELECT COUNT(*) FROM t_order").fetchone()[0]
    c.close()
    print(f"[seed] 新增订单 {len(NEW_ORDERS)} 笔，新增纠纷 {added} 条")
    print(f"[seed] 当前：订单 {total_o} 笔 / 纠纷 {total_d} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
