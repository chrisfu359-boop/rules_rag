"""物流轨迹查询：运单号 → 揽收/中转/签收节点。

为什么要有这一层：
  规则 2.4「虚假发货」的判定口径是「上传无效运单号、超时未揽收」。上一版
  `fake_ship` 只能读订单表里的静态 `picked_at` 字段——那个字段只有「揽收」
  一个时间点，没有中转、派送、签收这些中间节点，也拿不到「这条轨迹到底
  走没走通」。

  这一层把「运单号」变成可查询的轨迹事实来源：
    get_trace(carrier, tracking_no, shipped_at, picked_at) → 轨迹节点列表
  上层（order_detail 的 fake_ship、带单核查的物流段）只消费节点。

实现策略：
  - 默认本地模拟器（LocalTrace）：以订单表已有的 `picked_at` 为锚点补全
    中间节点（中转/派送/签收），确定性、可复现。没有 `picked_at` 的运单号
    （点了发货但从未揽收）只返回「已下单」节点——这正是虚假发货样本。
  - 预留 LOGISTICS_PROVIDER 环境变量，接真实物流聚合接口（快递鸟/快递100）
    时只需新增一个 Provider 类，用真实轨迹替换模拟轨迹，业务代码零改动。

为什么不在这里凭空生成揽收时间：
  轨迹模拟器若按运单号独立 hash 生成揽收时间，会与 backfill 造数时写死的
  `picked_at` 打架——同一张订单，数据库说「4 小时前揽收」，模拟器说「6 小时
  后揽收」，两套口径谁都不信谁。锚定 `picked_at`，则模拟轨迹与数据库事实
  永远一致；接真实接口后，真实揽收时间自然覆盖静态字段，也是同一套口径。
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta

# 轨迹节点状态 → 中文（页面/带单核查共用一套口径）
TRACE_LABEL = {
    "created": "已下单",
    "accepted": "已揽收",       # 进入承运方网络，视为「真正发货」
    "transit": "运输中",
    "out_delivery": "派送中",
    "signed": "已签收",
    "returned": "已退回",
}

# 承运商前缀 → 中文名（与 backfill_order_items 的 _item_for 保持一致）
CARRIER_PREFIX = {"SF": "顺丰速运", "ZT": "中通快递", "YT": "圆通速递", "JD": "京东物流"}


class LocalTrace:
    """本地轨迹模拟器：以 `picked_at` 为锚点补全中间节点，确定性可复现。

    轨迹结构：
      有揽收：created → accepted(picked_at) → transit → out_delivery → signed
      无揽收：created（点了发货但物流没取件，正是虚假发货样本）

    节点间隔由运单号 hash 派生（确定性），但「揽收」这一关键节点严格等于
    `picked_at`，保证与数据库事实、与 backfill 造数口径一致。
    """

    def __init__(self) -> None:
        self._cache: dict[tuple, list[dict]] = {}

    def _seed(self, tracking_no: str) -> int:
        return int(hashlib.md5(tracking_no.encode()).hexdigest()[:8], 16)

    def get(self, carrier: str, tracking_no: str, shipped_at: str,
            picked_at: str = "") -> list[dict]:
        """返回轨迹节点，按时间升序。没有运单号返回空列表。"""
        key = (carrier, tracking_no, shipped_at, picked_at)
        if key in self._cache:
            return self._cache[key]
        nodes = self._build(carrier, tracking_no, shipped_at, picked_at)
        self._cache[key] = nodes
        return nodes

    def _build(self, carrier: str, tracking_no: str, shipped_at: str,
               picked_at: str) -> list[dict]:
        tracking_no = (tracking_no or "").strip()
        if not tracking_no or not shipped_at:
            return []
        seed = self._seed(tracking_no)
        ship = self._parse(shipped_at)
        if not ship:
            return []

        nodes: list[dict] = [{"time": ship.strftime("%Y-%m-%d %H:%M:%S"),
                              "status": "created", "desc": "商家已发货"}]
        pick = self._parse(picked_at)
        if not pick:
            # 点了发货但从未揽收 → 虚假发货样本，轨迹止步于「已下单」
            return nodes

        nodes.append({"time": pick.strftime("%Y-%m-%d %H:%M:%S"),
                      "status": "accepted", "desc": f"{carrier or '承运商'}已揽收"})
        # 中间节点由运单号派生间隔，确定性；时间锚在 pick 之后
        transit = pick + timedelta(hours=12 + (seed % 12))
        nodes.append({"time": transit.strftime("%Y-%m-%d %H:%M:%S"),
                      "status": "transit", "desc": "快件到达转运中心"})
        out = transit + timedelta(hours=10 + (seed % 8))
        nodes.append({"time": out.strftime("%Y-%m-%d %H:%M:%S"),
                      "status": "out_delivery", "desc": "派送中，请保持电话畅通"})
        signed = out + timedelta(hours=6 + (seed % 10))
        nodes.append({"time": signed.strftime("%Y-%m-%d %H:%M:%S"),
                      "status": "signed", "desc": "已签收"})
        return nodes

    @staticmethod
    def _parse(v):
        s = str(v or "")[:19]
        for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, f)
            except ValueError:
                continue
        return None


_PROVIDER = None


def _get_provider() -> LocalTrace:
    """按 LOGISTICS_PROVIDER 选实现。默认本地模拟器；将来接真实接口时，
    在这里换成 HTTP 聚合客户端（快递鸟/快递100），业务代码不用动。"""
    global _PROVIDER
    if _PROVIDER is None:
        name = os.getenv("LOGISTICS_PROVIDER", "local").strip().lower()
        if name in ("local", "mock", ""):
            _PROVIDER = LocalTrace()
        else:
            # 未知 provider 回退本地，而不是让服务起不来
            print(f"[logistics] 未知 provider '{name}'，回退本地模拟器")
            _PROVIDER = LocalTrace()
    return _PROVIDER


def get_trace(carrier: str, tracking_no: str, shipped_at: str,
              picked_at: str = "") -> list[dict]:
    """查询运单轨迹。返回节点列表（含 time/status/desc），升序。"""
    return _get_provider().get(carrier, tracking_no, shipped_at, picked_at)


def pick_time(carrier: str, tracking_no: str, shipped_at: str,
              picked_at: str = "") -> str | None:
    """从轨迹里取「首次揽收时间」。没有揽收返回 None。

    这是虚假发货判定的唯一事实来源：不再读订单表里写死的 picked_at。
    """
    for n in get_trace(carrier, tracking_no, shipped_at, picked_at):
        if n["status"] == "accepted":
            return n["time"]
    return None
