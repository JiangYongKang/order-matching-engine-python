"""订单簿：按价格优先、时间优先维护买卖两侧挂单。

数据结构：side -> price -> deque[Order]（同价按到达顺序，即时间优先）。
"""
from __future__ import annotations

from collections import deque

from engine.models import Order, Side


class OrderBook:
    """单标的订单簿。买单价降序、卖单价升序，同价按 seq 升序（先到先撮合）。"""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._bids: dict[int, deque[Order]] = {}
        self._asks: dict[int, deque[Order]] = {}
        self._index: dict[str, Order] = {}

    def _levels(self, side: Side) -> dict[int, deque[Order]]:
        return self._bids if side is Side.BUY else self._asks

    def add(self, order: Order) -> None:
        assert order.order_id not in self._index
        self._levels(order.side).setdefault(order.price, deque()).append(order)
        self._index[order.order_id] = order

    def remove(self, order_id: str) -> Order | None:
        order = self._index.pop(order_id, None)
        if order is None:
            return None
        levels = self._levels(order.side)
        dq = levels[order.price]
        dq.remove(order)
        if not dq:
            del levels[order.price]
        return order

    def get(self, order_id: str) -> Order | None:
        return self._index.get(order_id)

    def best_bid(self) -> Order | None:
        if not self._bids:
            return None
        return self._bids[max(self._bids)][0]

    def best_ask(self) -> Order | None:
        if not self._asks:
            return None
        return self._asks[min(self._asks)][0]

    def iter_bids(self) -> list[Order]:
        """按撮合优先级返回买单序列（价高者优先，同价先到者优先）。"""
        return [o for p in sorted(self._bids, reverse=True) for o in self._bids[p]]

    def iter_asks(self) -> list[Order]:
        return [o for p in sorted(self._asks) for o in self._asks[p]]

    def __len__(self) -> int:
        return len(self._index)
