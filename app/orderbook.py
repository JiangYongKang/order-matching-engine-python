"""价格优先、时间优先的订单簿。

排序规则：
- 买单（bids）：价格降序，同价按 created_ns（下单序号）升序；
- 卖单（asks）：价格升序，同价按 created_ns 升序。
订单簿只持有未终止订单（remaining_qty > 0）。撮合由引擎驱动，
本类不含账务与落盘逻辑。
"""

from __future__ import annotations

from decimal import Decimal

from app.enums import OrderType, Side
from app.models import Order


def _insert_preserving(levels: list[Order], order: Order, reverse_price: bool):
    """按 (价格优先级, 时间优先级) 插入到正确位置，稳定保持时间优先。"""
    placed = False
    for i, other in enumerate(levels):
        if reverse_price:  # 买单：更高价在前
            if order.price > other.price:
                levels.insert(i, order)
                placed = True
                break
        else:              # 卖单：更低价在前
            if order.price < other.price:
                levels.insert(i, order)
                placed = True
                break
    if not placed:
        levels.append(order)


class OrderBook:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._bids: list[Order] = []
        self._asks: list[Order] = []

    # ---- 维护 -----------------------------------------------------------
    def add(self, order: Order) -> None:
        """限价单剩余量挂簿（市价单不挂簿）。"""
        if order.order_type is not OrderType.LIMIT or order.price is None:
            raise ValueError("只有限价单可挂簿")
        if order.is_terminal or order.remaining_qty <= 0:
            return
        if order.side is Side.BUY:
            _insert_preserving(self._bids, order, reverse_price=True)
        else:
            _insert_preserving(self._asks, order, reverse_price=False)

    def remove(self, order: Order) -> None:
        levels = self._bids if order.side is Side.BUY else self._asks
        try:
            levels.remove(order)
        except ValueError:
            # 已因成交移出等情形：幂等忽略。
            pass

    # ---- 查询 -----------------------------------------------------------
    def best_opposite(self, side: Side) -> Order | None:
        """取对手方最优：买单看最低卖价，卖单看最高买价。"""
        opp = self._asks if side is Side.BUY else self._bids
        return opp[0] if opp else None

    def can_match(self, taker: Order, maker: Order) -> bool:
        """价格条件。
        - 限价买：limit >= 卖一；限价卖：limit <= 买一；
        - 市价卖：无价格限制，只要有买一；
        - 市价买：保护价 max_price >= 卖一。
        """
        if taker.side is Side.BUY:
            if taker.order_type is OrderType.MARKET:
                return taker.max_price is not None and maker.price is not None \
                    and maker.price <= taker.max_price
            return maker.price is not None and taker.price is not None \
                and taker.price >= maker.price
        # taker SELL
        if taker.order_type is OrderType.MARKET:
            return maker.price is not None
        return maker.price is not None and taker.price is not None \
            and taker.price <= maker.price

    def iter_match(self, taker: Order):
        """惰性产出可成交的对手方 maker：按价格/时间优先级依次给出，
        一旦价格不再满足即停止（后续价位更差，无需继续）。

        本迭代器对“maker 是否已被消费/移除”健壮：yield 后若 maker 仍在
        原位则越过它；若调用方（引擎）已将其移除、后续元素前移，则不前进，
        从而每个未终止 maker 最多被产出一次，且不会死循环。
        """
        opp = self._asks if taker.side is Side.BUY else self._bids
        idx = 0
        while idx < len(opp):
            maker = opp[idx]
            if taker.remaining_qty <= 0:
                break
            if maker.is_terminal or maker.remaining_qty <= 0:
                opp.pop(idx)
                continue
            if not self.can_match(taker, maker):
                break
            yield maker
            if idx < len(opp) and opp[idx] is maker:
                idx += 1
            # maker 已被移除（前移）时保持 idx，检查新的队首。

    def levels(self) -> dict:
        def agg(levels: list[Order]) -> list[dict]:
            grouped: dict[Decimal, dict] = {}
            for o in levels:
                row = grouped.setdefault(
                    o.price,
                    {"price": o.price, "remaining_qty": Decimal(0),
                     "orders": []},
                )
                row["remaining_qty"] += o.remaining_qty
                row["orders"].append(
                    {"order_id": o.order_id, "qty": o.remaining_qty}
                )
            return list(grouped.values())

        return {
            "symbol": self.symbol,
            "bids": agg(self._bids),
            "asks": agg(self._asks),
        }
