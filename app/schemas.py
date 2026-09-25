"""HTTP 层 Pydantic 模型（金额/数量以 Decimal 字符串接收，避免浮点误差）。"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.enums import OrderStatus, OrderType, Side


class OrderSubmitRequest(BaseModel):
    # 注意：数量不在 Pydantic 层做正数限制，使“非正数量”能进入引擎风控，
    # 返回 400 与可区分原因 INVALID_QTY（而非 422 结构错误）。
    client_order_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    order_type: OrderType = OrderType.LIMIT
    qty: Decimal
    price: Decimal | None = Field(default=None)
    max_price: Decimal | None = Field(default=None)


class OrderView(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    order_id: str
    client_order_id: str
    account_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: Decimal
    price: Decimal | None
    max_price: Decimal | None
    status: OrderStatus
    filled_qty: Decimal
    avg_fill_price: Decimal
    remaining_qty: Decimal


class CancelResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    order_id: str
    canceled: bool
    status: OrderStatus
    canceled_qty: Decimal
    reason: str | None = None


class RejectResponse(BaseModel):
    error: str = "ORDER_REJECTED"
    code: str
    reason: str
    detail: str
