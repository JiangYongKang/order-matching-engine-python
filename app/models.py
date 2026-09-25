"""核心数据模型：订单、成交记录。

所有金额与数量均使用 Decimal，保证账务精确、守恒可验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.enums import OrderStatus, OrderType, Side


@dataclass
class Order:
    order_id: str
    client_order_id: str          # 幂等标识（账户维度唯一）
    account_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: Decimal                  # 原始委托量
    price: Decimal | None         # 限价单价格
    max_price: Decimal | None     # 市价买保护价
    status: OrderStatus
    filled_qty: Decimal = Decimal(0)
    avg_fill_price: Decimal = Decimal(0)
    created_at_ns: int = 0        # 单调纳秒时间戳，决定时间优先
    version: int = 0              # 状态变更版本号（终态校验用）

    @property
    def remaining_qty(self) -> Decimal:
        return self.qty - self.filled_qty

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED
        )


@dataclass
class Fill:
    """一笔成交（taker 与 maker 各视角由引擎展开记账）。

    taker_freeze_price / maker_freeze_price 为双方下单时的冻结价
    （限价单=限价，市价买=保护价），用于成交时把“冻结价优于成交价”
    的价差退回到可用资金，保证冻结不虚增、现金总量守恒。
    """

    trade_id: str
    symbol: str
    taker_order_id: str
    maker_order_id: str
    taker_account: str
    maker_account: str
    side: Side                    # taker 方向
    price: Decimal
    qty: Decimal
    ts_ns: int
    taker_freeze_price: Decimal = Decimal(0)
    maker_freeze_price: Decimal = Decimal(0)


@dataclass
class CancelResult:
    order_id: str
    canceled: bool
    status: OrderStatus
    canceled_qty: Decimal = Decimal(0)
    reason: str | None = None
