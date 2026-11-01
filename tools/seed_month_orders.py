"""补 11 月订单：让商家看板的「本月销售额 / 客单价」有数据。

背景：历史数据只到 10-31，而系统日期是 11-01，所以本月统计直接是 0。
注意数量要贴合真实日均：霞光历史 667 单 / 60 天 ≈ 11 单/天，
所以 11-01 只补 12 单，补 38 单会显得假（月初第一天就抵平时三天）。

用法：python tools/seed_month_orders.py
"""
from __future__ import annotations

import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "rules.db"
random.seed(20261101)

# 本月主推店铺（商家登录看到的就是霞光）
MAIN_SHOP = "霞光家居旗舰店"
OTHER_SHOPS = ["梦境数码专营店", "鲜达生鲜直营店", "运动装备仓",
               "潮流服饰工厂店", "母婴优选馆", "美妆小站旗舰店", "无名小店"]

GOODS = [
    ("实木餐桌 1.6m", "原木色 / 1.6m", 1899.0, "家具"),
    ("乳胶床垫 1.8m", "软硬双面", 2599.0, "家具"),
    ("四件套纯棉", "灰蓝 / 加大", 399.0, "家纺"),
    ("落地灯", "暖光 / 可调", 469.0, "灯具"),
    ("收纳柜", "五层 / 白色", 899.0, "家具"),
    ("加湿器", "4L 静音", 259.0, "小家电"),
    ("餐椅", "原木色 / 两把装", 799.0, "家具"),
    ("地毯", "1.6×2.3m", 599.0, "家纺"),
    ("书架", "六层 / 深木色", 1299.0, "家具"),
    ("香薰礼盒", "三件套", 329.0, "生活百货"),
]
CARRIERS = ["顺丰速运", "中通快递", "圆通速递", "韵达快递", "京东物流"]


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    seq = 1000
    added = 0

    def add_order(shop: str, buyer_id: int | None, ts: datetime, status: str) -> None:
        nonlocal seq, added
        title, sku, price, cat = random.choice(GOODS)
        qty = random.choices([1, 1, 1, 2], k=1)[0]
        amount = round(price * qty, 2)
        ono = f"SO20261101{seq:04d}"
        seq += 1
        paid = fmt(ts - timedelta(minutes=random.randint(1, 40))) if status != "待付款" else None
        shipped = fmt(ts + timedelta(hours=random.randint(2, 20))) \
            if status in ("已发货", "已完成", "售后中") else None
        received = fmt(ts + timedelta(days=2, hours=random.randint(1, 12))) \
            if status == "已完成" else None
        tracking = f"{random.choice(['SF', 'ZT', 'YT', 'YD'])}{random.randint(10**11, 10**12 - 1)}" \
            if shipped else ""
        c.execute(
            "INSERT INTO t_order (order_no, shop_name, category, amount, status, paid_at, "
            "shipped_at, received_at, item_title, sku, qty, unit_price, carrier, tracking_no, buyer_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ono, shop, cat, amount, status, paid, shipped, received, title, sku, qty, price,
             random.choice(CARRIERS) if shipped else "", tracking, buyer_id))
        c.execute(
            "INSERT INTO t_order_item (order_no, line_no, title, sku, qty, unit_price, "
            "refund_qty, refund_amount, category, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ono, 1, title, sku, qty, price, 0, 0, cat, paid or fmt(ts)))
        added += 1

    # 今天（11-01）是本月唯一一天，订单全部落在今天的不同时段
    for i in range(12):
        ts = today + timedelta(hours=8, minutes=i * 18)
        add_order(MAIN_SHOP, 5 if i % 2 == 0 else None, ts,
                  random.choices(["待发货", "已发货", "已完成"], [2, 3, 5])[0])
    # 其他店铺各补几单，平台总览也好看
    for i in range(4):
        ts = today + timedelta(hours=9, minutes=i * 25)
        add_order(OTHER_SHOPS[i % len(OTHER_SHOPS)], None, ts,
                  random.choices(["待发货", "已发货", "已完成"], [2, 3, 5])[0])

    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("PRAGMA journal_mode=DELETE")
    total = c.execute("SELECT COUNT(*) FROM t_order").fetchone()[0]
    c.close()
    print(f"[seed] 新增 {added} 笔 11 月订单（{MAIN_SHOP} 12 笔 + 其他店铺 4 笔）")
    print(f"[seed] 当前订单总数 {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
