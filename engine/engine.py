"""撮合引擎与订单服务：下单、撤单、查询、幂等、风控冻结、事件溯源恢复。

并发模型：所有变更操作在同一把可重入锁内完成，串行化下单/撤单/成交，
保证撤单与成交竞争时同一笔订单只能有一个终态；冻结检查与账务变更原子。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from engine.ledger import Ledger
from engine.models import (
    Order,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
    SubmitResult,
    Trade,
)
from engine.orderbook import OrderBook
from engine.store import EventStore

log = logging.getLogger("matching")


class MatchingEngine:
    """线程安全的内存撮合引擎。"""

    def __init__(self, symbols: list[str], wal_path: Path | None = None,
                 recover: bool = True) -> None:
        self._lock = threading.RLock()
        self._ledger = Ledger()
        self._books: dict[str, OrderBook] = {s: OrderBook(s) for s in symbols}
        self._symbols = set(symbols)
        self._orders: dict[str, Order] = {}
        self._trades: list[Trade] = []
        self._idempotency: dict[str, str] = {}          # key -> order_id
        self._rejections: dict[str, RejectReason] = {}  # key -> 拒绝原因
        self._store = EventStore(wal_path)
        self._seq = 0
        if recover:
            self._replay()

    # ---------- 内部工具 ----------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _order_to_event(self, o: Order) -> dict:
        return {
            "type": "ORDER_ACCEPTED",
            "order_id": o.order_id,
            "account_id": o.account_id,
            "symbol": o.symbol,
            "side": o.side.value,
            "order_type": o.order_type.value,
            "price": o.price,
            "quantity": o.quantity,
            "frozen_price": o.frozen_price,
            "idempotency_key": o.order_id,  # 占位，submit 时覆盖
            "seq": o.seq,
        }

    def _trade_to_event(self, t: Trade) -> dict:
        return {
            "type": "TRADE",
            "trade_id": t.trade_id,
            "s": t.symbol,
            "p": t.price,
            "q": t.quantity,
            "buy_order_id": t.buy_order_id,
            "sell_order_id": t.sell_order_id,
            "buy_account_id": t.buy_account_id,
            "sell_account_id": t.sell_account_id,
            "seq": t.seq,
        }

    # ---------- 资金/持仓注入（测试与初始化） ----------
    def deposit_cash(self, account_id: str, amount: int) -> None:
        with self._lock:
            self._ledger.deposit_cash(account_id, amount)
            self._store.append({"type": "DEPOSIT_CASH",
                                "account_id": account_id, "amount": amount})

    def deposit_position(self, account_id: str, symbol: str, qty: int) -> None:
        with self._lock:
            self._ledger.deposit_position(account_id, symbol, qty)
            self._store.append({"type": "DEPOSIT_POSITION", "account_id": account_id,
                                "symbol": symbol, "qty": qty})

    # ---------- 下单 ----------
    def submit_order(self, idempotency_key: str, account_id: str, symbol: str,
                     side: str, order_type: str, price: int, quantity: int) -> SubmitResult:
        with self._lock:
            # 1) 幂等命中：直接回放历史结论，不重新冻结/撮合/扣减
            if idempotency_key in self._idempotency:
                return self._duplicate_result(idempotency_key)
            if idempotency_key in self._rejections:
                reason = self._rejections[idempotency_key]
                log.info("order duplicate-reject key=%s account=%s reason=%s status=REJECTED",
                         idempotency_key, account_id, reason.value)
                return SubmitResult("", False, OrderStatus.REJECTED, reason,
                                    duplicate=True)

            # 2) 参数校验
            try:
                side_e = Side(side)
                type_e = OrderType(order_type)
            except ValueError:
                return self._reject(idempotency_key, account_id, symbol,
                                    RejectReason.INVALID_ORDER,
                                    f"side/type 非法 {side}/{order_type}")
            if symbol not in self._symbols:
                return self._reject(idempotency_key, account_id, symbol,
                                    RejectReason.UNKNOWN_SYMBOL, f"未知标的 {symbol}")
            if quantity <= 0 or (type_e is OrderType.LIMIT and price <= 0):
                return self._reject(idempotency_key, account_id, symbol,
                                    RejectReason.INVALID_ORDER,
                                    f"非法数量/价格 qty={quantity} price={price}")

            book = self._books[symbol]

            # 3) 风控：估值 -> 冻结（检查与变更原子，杜绝超额占用/超卖）
            frozen_price = 0
            if side_e is Side.BUY:
                if type_e is OrderType.MARKET:
                    best = book.best_ask()
                    if best is None:
                        return self._reject(idempotency_key, account_id, symbol,
                                            RejectReason.NO_LIQUIDITY,
                                            "市价买单对手方无报价，无法估算冻结额")
                    frozen_price = best.price
                else:
                    frozen_price = price
                if not self._ledger.freeze_cash(account_id, frozen_price * quantity):
                    return self._reject(idempotency_key, account_id, symbol,
                                        RejectReason.INSUFFICIENT_FUNDS,
                                        f"需冻结 {frozen_price * quantity} 分")
            else:
                if not self._ledger.freeze_position(account_id, symbol, quantity):
                    return self._reject(idempotency_key, account_id, symbol,
                                        RejectReason.INSUFFICIENT_POSITION,
                                        f"需冻结 {quantity} 股 {symbol}")

            # 4) 生成订单并落 WAL（先持久化"已接受"，再撮合）
            seq = self._next_seq()
            order = Order(
                order_id=f"O{seq}", account_id=account_id, symbol=symbol,
                side=side_e, order_type=type_e, price=price, quantity=quantity,
                remaining=quantity, seq=seq, frozen_price=frozen_price)
            self._orders[order.order_id] = order
            self._idempotency[idempotency_key] = order.order_id
            ev = self._order_to_event(order)
            ev["idempotency_key"] = idempotency_key
            self._store.append(ev)
            log.info("order accepted id=%s key=%s account=%s %s %s qty=%s price=%s",
                     order.order_id, idempotency_key, account_id, side_e.value,
                     type_e.value, quantity, price)

            # 5) 撮合
            trades = self._match(order)

            # 6) 剩余处理
            if order.remaining > 0 and type_e is OrderType.MARKET:
                # 市价单(IOC)：未成交部分立即撤销并释放冻结
                self._cancel_remaining(order, reason="MARKET_IOC", log_it=False)
            elif order.remaining > 0:
                book.add(order)
                if order.remaining < order.quantity:
                    order.status = OrderStatus.PARTIALLY_FILLED
                log.info("order resting id=%s remaining=%s status=%s",
                         order.order_id, order.remaining, order.status.value)
            else:
                order.status = OrderStatus.FILLED

            result = SubmitResult(order.order_id, True, order.status,
                                  RejectReason.NONE, trades)
            log.info("order decision id=%s key=%s filled=%s/%s status=%s trades=%d",
                     order.order_id, idempotency_key,
                     order.quantity - order.remaining, order.quantity,
                     order.status.value, len(trades))
            return result

    def _reject(self, key: str, account_id: str, symbol: str,
                reason: RejectReason, detail: str) -> SubmitResult:
        self._rejections[key] = reason
        self._store.append({"type": "ORDER_REJECTED", "idempotency_key": key,
                            "account_id": account_id, "symbol": symbol,
                            "reason": reason.value})
        log.info("order rejected key=%s account=%s symbol=%s reason=%s detail=%s",
                 key, account_id, symbol, reason.value, detail)
        return SubmitResult("", False, OrderStatus.REJECTED, reason)

    def _duplicate_result(self, key: str) -> SubmitResult:
        oid = self._idempotency[key]
        order = self._orders[oid]
        trades = [t for t in self._trades
                  if t.buy_order_id == oid or t.sell_order_id == oid]
        log.info("order duplicate key=%s id=%s status=%s trades=%d (no re-execution)",
                 key, oid, order.status.value, len(trades))
        return SubmitResult(oid, True, order.status, RejectReason.NONE,
                            trades, duplicate=True)

    # ---------- 撮合 ----------
    def _match(self, order: Order) -> list[Trade]:
        book = self._books[order.symbol]
        makers = book.iter_asks() if order.side is Side.BUY else book.iter_bids()
        trades: list[Trade] = []
        for maker in makers:
            if order.remaining == 0:
                break
            if order.order_type is OrderType.LIMIT:
                if order.side is Side.BUY and maker.price > order.price:
                    break
                if order.side is Side.SELL and maker.price < order.price:
                    break
            qty = min(order.remaining, maker.remaining)
            price = maker.price  # 成交价取挂单价（maker price）
            if order.side is Side.BUY:
                buy, sell = order, maker
            else:
                buy, sell = maker, order

            self._ledger.settle_trade(
                buy.account_id, sell.account_id, order.symbol,
                price, qty, buy.frozen_price)

            seq = self._next_seq()
            trade = Trade(
                trade_id=f"T{seq}", symbol=order.symbol, price=price, quantity=qty,
                buy_order_id=buy.order_id, sell_order_id=sell.order_id,
                buy_account_id=buy.account_id, sell_account_id=sell.account_id, seq=seq)
            self._trades.append(trade)
            self._store.append(self._trade_to_event(trade))

            order.remaining -= qty
            maker.remaining -= qty
            maker.status = (OrderStatus.FILLED if maker.remaining == 0
                            else OrderStatus.PARTIALLY_FILLED)
            if maker.remaining == 0:
                book.remove(maker.order_id)
            trades.append(trade)
            log.info("trade id=%s %s price=%s qty=%s buyer=%s seller=%s remaining_taker=%s",
                     trade.trade_id, order.symbol, price, qty,
                     buy.order_id, sell.order_id, order.remaining)
        if order.remaining == 0:
            order.status = OrderStatus.FILLED
        return trades

    # ---------- 撤单 ----------
    def cancel_order(self, order_id: str) -> OrderStatus:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise KeyError(f"订单不存在 {order_id}")
            if order.status.is_terminal:
                log.info("cancel skip id=%s status=%s (terminal)",
                         order_id, order.status.value)
                return order.status
            self._cancel_remaining(order, reason="USER_CANCEL", log_it=True)
            return order.status

    def _cancel_remaining(self, order: Order, reason: str, log_it: bool) -> None:
        """释放剩余冻结并置终态 CANCELLED。调用方必须持锁，且订单非终态。"""
        if order.status.is_terminal:
            return
        self._books[order.symbol].remove(order.order_id)
        if order.side is Side.BUY:
            self._ledger.unfreeze_cash(
                order.account_id, order.frozen_price * order.remaining)
        else:
            self._ledger.unfreeze_position(
                order.account_id, order.symbol, order.remaining)
        order.status = OrderStatus.CANCELLED
        self._store.append({"type": "ORDER_CANCELLED", "order_id": order.order_id,
                            "reason": reason, "remaining": order.remaining})
        if log_it:
            log.info("order cancelled id=%s remaining=%s reason=%s",
                     order.order_id, order.remaining, reason)

    # ---------- 查询 ----------
    def get_order(self, order_id: str) -> Order | None:
        with self._lock:
            return self._orders.get(order_id)

    def list_trades(self, symbol: str | None = None) -> list[Trade]:
        with self._lock:
            if symbol is None:
                return list(self._trades)
            return [t for t in self._trades if t.symbol == symbol]

    def snapshot_account(self, account_id: str) -> dict:
        with self._lock:
            acc = self._ledger.get_or_create(account_id)
            return {
                "account_id": account_id,
                "cash_available": acc.cash_available,
                "cash_frozen": acc.cash_frozen,
                "positions": {s: {"available": p.available, "frozen": p.frozen}
                              for s, p in acc.positions.items()},
            }

    def total_cash(self) -> int:
        with self._lock:
            return self._ledger.total_cash()

    def total_position(self, symbol: str) -> int:
        with self._lock:
            return self._ledger.total_position(symbol)

    # ---------- 事件回放恢复 ----------
    def _replay(self) -> None:
        events = self._store.load_all()
        for e in events:
            self._apply(e)
        if events:
            log.info("recovery replayed=%d orders=%d trades=%d seq=%d",
                     len(events), len(self._orders), len(self._trades), self._seq)

    def _apply(self, e: dict) -> None:
        t = e["type"]
        if t == "DEPOSIT_CASH":
            self._ledger.deposit_cash(e["account_id"], e["amount"])
        elif t == "DEPOSIT_POSITION":
            self._ledger.deposit_position(e["account_id"], e["symbol"], e["qty"])
        elif t == "ORDER_ACCEPTED":
            order = Order(
                order_id=e["order_id"], account_id=e["account_id"], symbol=e["symbol"],
                side=Side(e["side"]), order_type=OrderType(e["order_type"]),
                price=e["price"], quantity=e["quantity"], remaining=e["quantity"],
                status=OrderStatus.ACCEPTED, seq=e["seq"],
                frozen_price=e["frozen_price"])
            if order.side is Side.BUY:
                assert self._ledger.freeze_cash(
                    order.account_id, order.frozen_price * order.quantity)
            else:
                assert self._ledger.freeze_position(
                    order.account_id, order.symbol, order.quantity)
            self._orders[order.order_id] = order
            self._books[order.symbol].add(order)
            self._idempotency[e["idempotency_key"]] = order.order_id
        elif t == "ORDER_REJECTED":
            self._rejections[e["idempotency_key"]] = RejectReason(e["reason"])
        elif t == "TRADE":
            buy = self._orders[e["buy_order_id"]]
            sell = self._orders[e["sell_order_id"]]
            qty = e["q"]
            self._ledger.settle_trade(
                buy.account_id, sell.account_id, e["s"], e["p"], qty,
                buy.frozen_price)
            for o in (buy, sell):
                o.remaining -= qty
                o.status = (OrderStatus.FILLED if o.remaining == 0
                            else OrderStatus.PARTIALLY_FILLED)
                if o.remaining == 0:
                    self._books[o.symbol].remove(o.order_id)
            trade = Trade(
                trade_id=e["trade_id"], symbol=e["s"], price=e["p"], quantity=qty,
                buy_order_id=buy.order_id, sell_order_id=sell.order_id,
                buy_account_id=buy.account_id, sell_account_id=sell.account_id,
                seq=e["seq"])
            self._trades.append(trade)
        elif t == "ORDER_CANCELLED":
            order = self._orders[e["order_id"]]
            if not order.status.is_terminal:
                self._books[order.symbol].remove(order.order_id)
                if order.side is Side.BUY:
                    self._ledger.unfreeze_cash(
                        order.account_id, order.frozen_price * order.remaining)
                else:
                    self._ledger.unfreeze_position(
                        order.account_id, order.symbol, order.remaining)
                order.status = OrderStatus.CANCELLED
        else:
            raise ValueError(f"未知事件类型 {t}")
        if "seq" in e:
            self._seq = max(self._seq, e["seq"])
