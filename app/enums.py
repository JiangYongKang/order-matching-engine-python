"""枚举与拒绝原因定义。"""

from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    NEW = "NEW"                       # 已接受，尚未成交
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 部分成交，剩余量在簿
    FILLED = "FILLED"                 # 全部成交（终态）
    CANCELED = "CANCELED"             # 已撤销（终态）
    REJECTED = "REJECTED"             # 风控拒绝（终态）


TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
)
LIVE_STATUSES = frozenset({OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED})


class EventType(str, Enum):
    ACCOUNT_OPENED = "ACCOUNT_OPENED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELED = "ORDER_CANCELED"
    ORDER_REJECTED = "ORDER_REJECTED"


class RejectReason(str, Enum):
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_QTY = "INVALID_QTY"
    MARKET_BUY_NEEDS_PROTECTION = "MARKET_BUY_NEEDS_PROTECTION"
    INSUFFICIENT_AVAILABLE_CASH = "INSUFFICIENT_AVAILABLE_CASH"
    INSUFFICIENT_AVAILABLE_POSITION = "INSUFFICIENT_AVAILABLE_POSITION"


REJECT_MESSAGES: dict[RejectReason, str] = {
    RejectReason.UNKNOWN_ACCOUNT: "账户不存在",
    RejectReason.UNKNOWN_SYMBOL: "交易品种不存在",
    RejectReason.INVALID_PRICE: "价格非法（限价单需为正数）",
    RejectReason.INVALID_QTY: "数量非法（需为正数）",
    RejectReason.MARKET_BUY_NEEDS_PROTECTION:
        "市价买单必须提供保护价 max_price",
    RejectReason.INSUFFICIENT_AVAILABLE_CASH: "可用资金不足",
    RejectReason.INSUFFICIENT_AVAILABLE_POSITION: "可用持仓不足",
}


class OrderRejected(Exception):
    """风控拒单异常，携带可区分的拒绝原因。"""

    def __init__(self, reason: RejectReason, detail: str | None = None) -> None:
        self.reason = reason
        self.detail = detail or REJECT_MESSAGES[reason]
        super().__init__(f"{reason.value}: {self.detail}")


class CancelFailReason(str, Enum):
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    NO_REMAINING_QTY = "NO_REMAINING_QTY"
