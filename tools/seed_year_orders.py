"""补 1-8 月历史订单：让「本年」和「日销」自洽。

问题：原来数据只覆盖 9-10 月两个月，按「本年」统计出来只有 640 单，
但 11-01 一天就 12 单 —— 12×365≈4380 单/年，和 640 差一个量级，一看就假。

做法：按每家店的日销水平补齐 2026-01-01 ~ 2026-08-31 的订单，
让「本年 ≈ 日销 × 365」。历史订单状态基本都是已完成。

用法：
    python tools/seed_year_orders.py
    然后重跑同步脚本把数据推到 MySQL。
"""
from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(r"G:\hm_st\sf_zj\code\coze_dify\homework\project\rules_rag")
DB = ROOT / "data" / "rules.db"
random.seed(20260101)

# 每家店的日销水平（单/天）：霞光按 12，其余按 8~10，和现有数据量级一致
SHOPS = {
    "霞光家居旗舰店": 12,
    "梦境数码专营店": 10,
    "无名小店": 6,
    "鲜达生鲜直营店": 9,
    "美妆小站旗舰店": 8,
    "潮流服饰工厂店": 9,
    "母婴优选馆": 7,
    "运动装备仓": 8,
}
GOODS = {
    "霞光家居旗舰店": [("实木餐桌 1.6m", 1899.0, "家具"), ("乳胶床垫 1.8m", 2599.0, "家具"),
                       ("四件套纯棉", 399.0, "家纺"), ("落地灯", 469.0, "灯具"),
                       ("收纳柜", 899.0, "家具"), ("餐椅两把装", 799.0, "家具")],
    "梦境数码专营店": [("无线降噪耳机", 599.0, "数码"), ("机械键盘", 349.0, "电脑外设"),
                       ("移动电源", 159.0, "数码"), ("显示器 27 寸", 1299.0, "电脑外设")],
    "无名小店": [("帆布袋", 59.0, "百货"), ("水杯", 39.0, "百货"), ("笔记本", 29.0, "文具")],
    "鲜达生鲜直营店": [("有机牛奶 24 盒", 138.0, "食品生鲜"), ("车厘子 2kg", 199.0, "食品生鲜"),
                       ("五常大米 10kg", 139.0, "食品生鲜")],
    "美妆小站旗舰店": [("口红礼盒", 329.0, "美妆"), ("精华液", 459.0, "美妆"),
                       ("面膜 20 片", 129.0, "美妆")],
    "潮流服饰工厂店": [("卫衣", 239.0, "服饰"), ("牛仔裤", 299.0, "服饰"), ("羽绒服", 899.0, "服饰")],
    "母婴优选馆": [("纸尿裤 3 包", 199.0, "母婴"), ("奶粉 2 罐", 398.0, "母婴")],
    "运动装备仓": [("瑜伽垫", 89.0, "运动户外"), ("跑鞋", 559.0, "运动户外"), ("哑铃 20kg", 299.0, "运动户外")],
}
CARRIERS = ["顺丰速运", "中通快递", "圆通速递", "韵达快递", "京东物流"]


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    c = sqlite3.connect(DB)
    start = datetime(2026, 1, 1)
    end = datetime(2026, 8, 31)
    seq = 0
    orders, items = [], []

    day = start
    while day <= end:
        for shop, rate in SHOPS.items():
            n = max(1, random.randint(rate - 3, rate + 3))
            for _ in range(n):
                title, price, cat = random.choice(GOODS[shop])
                qty = random.choices([1, 1, 1, 2], k=1)[0]
                amount = round(price * qty, 2)
                seq += 1
                ono = f"SO{day.strftime('%Y%m%d')}{seq:06d}"
                ts = day + timedelta(hours=random.randint(8, 21), minutes=random.randint(0, 59))
                paid = fmt(ts - timedelta(minutes=random.randint(1, 30)))
                shipped = fmt(ts + timedelta(hours=random.randint(2, 20)))
                received = fmt(ts + timedelta(days=2, hours=random.randint(1, 12)))
                tracking = f"{random.choice(['SF', 'ZT', 'YT', 'YD'])}{random.randint(10**11, 10**12 - 1)}"
                orders.append((ono, shop, cat, amount, "已完成", paid, shipped, received,
                               title, "默认规格", qty, price, random.choice(CARRIERS), tracking,
                               5 if random.random() < 0.12 else None))
                items.append((ono, 1, title, "默认规格", qty, price, 0, 0, cat, paid))
        day += timedelta(days=1)

    c.executemany(
        "INSERT INTO t_order (order_no, shop_name, category, amount, status, paid_at, "
        "shipped_at, received_at, item_title, sku, qty, unit_price, carrier, tracking_no, buyer_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", orders)
    c.executemany(
        "INSERT INTO t_order_item (order_no, line_no, title, sku, qty, unit_price, "
        "refund_qty, refund_amount, category, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", items)
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("PRAGMA journal_mode=DELETE")
    total = c.execute("SELECT COUNT(*) FROM t_order").fetchone()[0]
    c.close()
    print(f"[seed] 新增 {len(orders)} 笔历史订单（2026-01-01 ~ 2026-08-31），总数 {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
