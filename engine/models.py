"""核心数据模型：订单、成交、枚举类型。仅类型定义，无业务逻辑。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Side(enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(enum.Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(enum.Enum):
    """成交方式。下单不传时按订单类型取默认：限价单=GTC，市价单=IOC（兼容旧行为）。"""
    GTC = "GTC"   # 立即成交，剩余继续挂单（仅限价单）
    IOC = "IOC"   # 立即成交，剩余部分自动撤销
    FOK = "FOK"   # 立即全部成交，否则整单不成交（原子失败，不留半成交）


class OrderStatus(enum.Enum):
    ACCEPTED = "ACCEPTED"            # 已接受，未成交，挂盘中
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 部分成交，剩余挂盘
    FILLED = "FILLED"                # 全部成交（终态）
    CANCELLED = "CANCELLED"          # 已撤销（终态）
    REJECTED = "REJECTED"            # 风控/校验拒绝（终态）

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


class RejectReason(enum.Enum):
    NONE = "NONE"
    DUPLICATE_IDEMPOTENCY_KEY = "DUPLICATE_IDEMPOTENCY_KEY"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    NO_LIQUIDITY = "NO_LIQUIDITY"          # 市价单对手方无流动性可估价
    FOK_UNFILLABLE = "FOK_UNFILLABLE"      # FOK 单流动性不足，整单不成交（原子失败）
    INVALID_ORDER = "INVALID_ORDER"        # 数量/价格非法
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"


@dataclass
class Order:
    order_id: str
    account_id: str
    symbol: str
    side: Side
    order_type: OrderType
    price: int          # 限价，单位：分；市价单为 0
    quantity: int       # 原始数量
    remaining: int      # 剩余未成交数量
    status: OrderStatus = OrderStatus.ACCEPTED
    seq: int = 0        # 全局序号，时间优先依据
    reject_reason: RejectReason = RejectReason.NONE
    frozen_price: int = 0  # 买单冻结单价（限价=price，市价=价格保护价）；卖单为 0
    time_in_force: TimeInForce | None = None  # 实际生效的成交方式（默认按类型解析）
    protection_price: int = 0  # 市价单价格保护：买=可接受上限，卖=可接受下限；0=未指定


@dataclass
class Trade:
    trade_id: str
    symbol: str
    price: int
    quantity: int
    buy_order_id: str
    sell_order_id: str
    buy_account_id: str
    sell_account_id: str
    seq: int = 0


@dataclass
class SubmitResult:
    order_id: str
    accepted: bool
    status: OrderStatus
    reject_reason: RejectReason = RejectReason.NONE
    trades: list[Trade] = field(default_factory=list)
    duplicate: bool = False   # 幂等命中：本次为重复提交，未重新执行
