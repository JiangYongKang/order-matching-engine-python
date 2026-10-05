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
    GTC = "GTC"  # 立即成交，剩余部分挂入订单簿
    IOC = "IOC"  # 立即成交，剩余部分自动撤销
    FOK = "FOK"  # 立即全部成交，否则整单不成交（拒单）


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
    FOK_NOT_FULLY_FILLABLE = "FOK_NOT_FULLY_FILLABLE"  # FOK 无法立即全部成交，整单失败
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
    frozen_price: int = 0  # 买单冻结单价（限价=price，市价=有效价格上限）；卖单为 0
    # 成交方式。下单未指定时按旧默认归一化：LIMIT→GTC，MARKET→IOC。
    time_in_force: TimeInForce = TimeInForce.GTC
    # 市价单价格保护（分）：买单=可接受最高成交价，卖单=可接受最低成交价；0=未指定。
    # 市价买未指定时有效上限=下单时最优卖价（即 frozen_price），保证成交总额不超冻结额；
    # 市价卖未指定时有效下限=0（不约束）。
    protection_price: int = 0


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
