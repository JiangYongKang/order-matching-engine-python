"""撮合引擎：串行化并发请求，保证账务与撮合结论一致、可解释。

并发模型
--------
所有写操作（submit_order / cancel_order）在同一把可重入锁内，原子完成
「风控判定 → 冻结 → 落盘 → 撮合 → 记账 → 落盘」全过程；读操作在同锁内
取快照。因此：
- 并发下单不会超卖或超额占用（冻结判定与扣减在同一临界区）；
- 撤单与成交竞争时，同一笔订单只会落到一个终态（FILLED/CANCELED 互斥）；
- 重复幂等键返回同一订单结论，不会重复成交或重复扣减。

价格/时间优先
-------------
撮合由 OrderBook 保证价格优先、同价时间优先；允许部分成交，限价单
剩余量继续挂簿，市价单不挂簿（剩余量作废）。

幂等与恢复
----------
幂等键为 (account_id, client_order_id)。订单接受与每次成交、撤单均写入
仅追加事件日志（fsync）。recover() 顺序重放事件即可重建账户余额、冻结、
订单状态与订单簿，结论与重启前一致。
"""

from __future__ import annotations

import itertools
import logging
import threading
import uuid
from decimal import Decimal

from app.accounts import Account, AccountBook
from app.enums import (
    CancelFailReason,
    EventType,
    OrderRejected,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
)
from app.log_store import EventLog
from app.models import CancelResult, Fill, Order
from app.orderbook import OrderBook

logger = logging.getLogger("app.engine")
ZERO = Decimal(0)


def _order_to_dict(o: Order) -> dict:
    return {
        "order_id": o.order_id,
        "client_order_id": o.client_order_id,
        "account_id": o.account_id,
        "symbol": o.symbol,
        "side": o.side.value,
        "order_type": o.order_type.value,
        "qty": str(o.qty),
        "price": None if o.price is None else str(o.price),
        "max_price": None if o.max_price is None else str(o.max_price),
        "status": o.status.value,
        "filled_qty": str(o.filled_qty),
        "avg_fill_price": str(o.avg_fill_price),
        "created_at_ns": o.created_at_ns,
        "version": o.version,
    }


def _order_from_dict(d: dict) -> Order:
    return Order(
        order_id=d["order_id"],
        client_order_id=d["client_order_id"],
        account_id=d["account_id"],
        symbol=d["symbol"],
        side=Side(d["side"]),
        order_type=OrderType(d["order_type"]),
        qty=Decimal(d["qty"]),
        price=None if d.get("price") is None else Decimal(d["price"]),
        max_price=None if d.get("max_price") is None else Decimal(d["max_price"]),
        status=OrderStatus(d["status"]),
        filled_qty=Decimal(d.get("filled_qty", "0")),
        avg_fill_price=Decimal(d.get("avg_fill_price", "0")),
        created_at_ns=d.get("created_at_ns", 0),
        version=d.get("version", 0),
    )


class MatchingEngine:
    def __init__(self, log: EventLog, symbols: list[str]) -> None:
        self._lock = threading.RLock()
        self.log = log
        self.symbols: set[str] = set(symbols)
        self.accounts = AccountBook()
        self.books: dict[str, OrderBook] = {s: OrderBook(s) for s in symbols}
        self.orders_by_id: dict[str, Order] = {}
        # (account_id, client_order_id) -> order_id
        self.idempotency: dict[tuple[str, str], str] = {}
        self._order_counter = itertools.count(1)

    # ---- 标识 -----------------------------------------------------------
    def _new_order_id(self) -> str:
        return f"O{next(self._order_counter):08d}"

    def _new_trade_id(self) -> str:
        return f"T-{uuid.uuid4().hex[:16]}"

    # ---- 开户 -----------------------------------------------------------
    def create_account(
        self,
        account_id: str,
        cash: Decimal = ZERO,
        positions: dict[str, Decimal] | None = None,
    ) -> None:
        """开户并记入事件日志（重启后初始资金/持仓可重建）。"""
        with self._lock:
            if self.accounts.get(account_id) is not None:
                raise ValueError(f"账户已存在: {account_id}")
            self.log.append(EventType.ACCOUNT_OPENED, {
                "account_id": account_id,
                "cash": str(Decimal(cash)),
                "positions": {s: str(Decimal(q))
                              for s, q in (positions or {}).items()},
            })
            self.accounts.create_account(account_id, Decimal(cash),
                                         positions or {})
            logger.info("开户 account=%s cash=%s positions=%s",
                        account_id, cash, positions or {})

    # ---- 恢复 -----------------------------------------------------------
    def recover(self) -> None:
        """从事件日志按原始顺序单遍重放，重建账户、订单与订单簿。

        严格按事件 seq（即真实发生顺序）处理：
          ACCOUNT_OPENED -> 建账户；
          ORDER_ACCEPTED -> 以零成交 NEW 重建订单、迁移冻结，并**立即**按
                           当时订单簿重放撮合（撮合与接受本就在同一临界区）；
          ORDER_FILLED   -> 账务已在上面的撮合重放中记一次，这里跳过；
          ORDER_CANCELED -> 在该事件的真实时间点释放剩余冻结并置终态；
          ORDER_REJECTED -> 不改余额。
        重放撮合推导出的成交若已在日志中（键匹配）则只记账不重复落盘；
        若不在（“接受已 fsync、成交未 fsync”的崩溃窗口），则补记账并以
        recovered=true 落盘。因此重启结论与崩溃前一致且不重不漏。
        """
        with self._lock:
            events = self.log.read_all()

            # 预收集：已落盘成交键、已落盘撤单订单（用于区分崩溃窗口）。
            persisted_fills: set[tuple] = set()
            canceled_ids: set[str] = set()
            for e in events:
                t = EventType(e["type"]); q = e["payload"]
                if t is EventType.ORDER_FILLED:
                    persisted_fills.add((
                        q["taker_order_id"], q["maker_order_id"],
                        str(q["price"]), str(q["qty"])))
                elif t is EventType.ORDER_CANCELED:
                    canceled_ids.add(q["order_id"])

            self.accounts = AccountBook()
            self.books = {s: OrderBook(s) for s in self.symbols}
            self.orders_by_id = {}
            self.idempotency = {}
            max_ns = 0

            for event in events:
                etype = EventType(event["type"])
                p = event["payload"]

                if etype is EventType.ACCOUNT_OPENED:
                    self.accounts.create_account(
                        p["account_id"], Decimal(p["cash"]),
                        {s: Decimal(q)
                         for s, q in p.get("positions", {}).items()})

                elif etype is EventType.ORDER_ACCEPTED:
                    order = _order_from_dict(p["order"])
                    order.status = OrderStatus.NEW
                    order.filled_qty = ZERO
                    order.avg_fill_price = ZERO
                    order.version = 0
                    self.orders_by_id[order.order_id] = order
                    self.idempotency[
                        (order.account_id, order.client_order_id)
                    ] = order.order_id
                    acct = self.accounts.require(order.account_id)
                    self._apply_frozen_snapshot(acct, p["frozen"])
                    max_ns = max(max_ns, order.created_at_ns)

                    # 接受与撮合处于同一临界区：立即按当前簿重放撮合。
                    freeze_price = self._freeze_price(order)
                    self._match(order, freeze_price, recovered=True,
                                persisted_fills=persisted_fills,
                                log_recovered=True)

                    if (order.remaining_qty > 0
                            and order.order_type is OrderType.LIMIT):
                        self.books[order.symbol].add(order)
                    # 市价剩余：若有落盘撤单事件，交给该事件释放；
                    # 否则是崩溃窗口丢失的作废，末尾统一补。
                # 成交已在撮合重放中记一次账，跳过。
                elif etype is EventType.ORDER_FILLED:
                    max_ns = max(max_ns, int(p.get("ts_ns", 0)))

                elif etype is EventType.ORDER_CANCELED:
                    order = self.orders_by_id[p["order_id"]]
                    if not order.is_terminal:
                        remaining = order.remaining_qty
                        if remaining > 0:
                            acct = self.accounts.require(order.account_id)
                            self.accounts.release_cancel(
                                acct, order.symbol, order.side, remaining,
                                Decimal(p.get("freeze_price", "0")))
                        if order.order_type is OrderType.LIMIT:
                            self.books[order.symbol].remove(order)
                        order.status = OrderStatus.CANCELED
                        order.version += 1

                elif etype is EventType.ORDER_REJECTED:
                    pass

            # 崩溃窗口补遗：市价单接受已落盘、但其剩余作废事件未落盘，
            # 且当前确实没有流动性（重放后仍零成交），补记作废并释放冻结。
            for order in list(self.orders_by_id.values()):
                if (not order.is_terminal
                        and order.order_type is OrderType.MARKET
                        and order.order_id not in canceled_ids
                        and order.remaining_qty > 0):
                    self._expire_market_remainder(
                        order, self._freeze_price(order), recovered=True)

            self._order_counter = itertools.count(max_ns + 1)
            logger.info("状态恢复完成：账户 %d 个，订单 %d 笔",
                        len(self.accounts._accounts), len(self.orders_by_id))

    def _apply_frozen_snapshot(self, acct: Account, frozen: dict) -> None:
        """重放 ORDER_ACCEPTED 时按其冻结快照恢复可用/冻结余额。

        由于 ACCOUNT_OPENED 已给出初始总额，这里对“接受时的冻结额”做一次性
        迁移（可用 -> 冻结），后续成交/撤单事件再逐步结清。
        """
        cash_frozen = Decimal(frozen.get("cash_frozen", "0"))
        if cash_frozen > 0:
            acct.cash_available -= cash_frozen
            acct.cash_frozen += cash_frozen
        for symbol, qty in frozen.get("position_frozen", {}).items():
            q = Decimal(qty)
            acct.position_available[symbol] = (
                acct.position_available.get(symbol, ZERO) - q
            )
            acct.position_frozen[symbol] = (
                acct.position_frozen.get(symbol, ZERO) + q
            )

    @staticmethod
    def _advance_on_fill(order: Order, qty: Decimal, price: Decimal) -> None:
        total_cost = order.avg_fill_price * order.filled_qty + price * qty
        order.filled_qty += qty
        order.avg_fill_price = (
            total_cost / order.filled_qty if order.filled_qty > 0 else ZERO
        )
        order.status = (
            OrderStatus.FILLED if order.remaining_qty <= 0
            else OrderStatus.PARTIALLY_FILLED
        )
        order.version += 1

    # ---- 下单 -----------------------------------------------------------
    @staticmethod
    def _freeze_price(order: Order) -> Decimal:
        """买单下单冻结价：限价=限价，市价=保护价；卖单不冻资金返回 0。"""
        if order.side is Side.BUY:
            if order.order_type is OrderType.LIMIT:
                return order.price
            return order.max_price
        return ZERO

    def _validate(
        self, *, account_id: str, symbol: str, side: Side,
        order_type: OrderType, qty: Decimal,
        price: Decimal | None, max_price: Decimal | None,
    ) -> None:
        if self.accounts.get(account_id) is None:
            raise OrderRejected(RejectReason.UNKNOWN_ACCOUNT)
        if symbol not in self.symbols:
            raise OrderRejected(
                RejectReason.UNKNOWN_SYMBOL, f"未知品种: {symbol}")
        if qty is None or qty <= 0:
            raise OrderRejected(RejectReason.INVALID_QTY)
        if order_type is OrderType.LIMIT:
            if price is None or price <= 0:
                raise OrderRejected(RejectReason.INVALID_PRICE)
        else:  # MARKET
            if side is Side.BUY and (max_price is None or max_price <= 0):
                raise OrderRejected(
                    RejectReason.MARKET_BUY_NEEDS_PROTECTION)

    def submit_order(
        self,
        *,
        client_order_id: str,
        account_id: str,
        symbol: str,
        side: Side,
        order_type: OrderType,
        qty: Decimal,
        price: Decimal | None = None,
        max_price: Decimal | None = None,
    ) -> Order:
        """提交订单。

        - 风控不通过：抛 OrderRejected（原因可区分），并写 ORDER_REJECTED；
        - 重复幂等键：直接返回原订单，不重复冻结/成交/扣减；
        - 成功：冻结→落盘接受→撮合记账→逐笔落盘成交，限价剩余挂簿。
        """
        with self._lock:
            # 幂等：相同 (账户, client_order_id) 返回同一结论。
            existing_id = self.idempotency.get((account_id, client_order_id))
            if existing_id is not None:
                existing = self.orders_by_id[existing_id]
                logger.info(
                    "幂等命中 order=%s client=%s 判定=重复提交 返回原订单 最终状态=%s",
                    existing.order_id, client_order_id, existing.status.value)
                return existing

            try:
                self._validate(
                    account_id=account_id, symbol=symbol, side=side,
                    order_type=order_type, qty=qty,
                    price=price, max_price=max_price)
            except OrderRejected as rej:
                self.log.append(EventType.ORDER_REJECTED, {
                    "account_id": account_id,
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side.value,
                    "reason": rej.reason.value,
                    "detail": rej.detail,
                })
                logger.info(
                    "风控拒单 account=%s client=%s 判定=%s 依据=%s 最终状态=REJECTED",
                    account_id, client_order_id, rej.reason.value, rej.detail)
                raise

            account = self.accounts.require(account_id)
            created_ns = next(self._order_counter)
            order = Order(
                order_id=self._new_order_id(),
                client_order_id=client_order_id,
                account_id=account_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                qty=qty,
                price=price,
                max_price=max_price,
                status=OrderStatus.NEW,
                created_at_ns=created_ns,
            )
            freeze_price = self._freeze_price(order)

            # 冻结（判定与扣减在锁内原子完成，杜绝超额占用）。
            cash_frozen = self.accounts.freeze_for_order(
                account, symbol, side, qty, freeze_price)
            frozen_snapshot = {
                "cash_frozen": str(cash_frozen),
                "position_frozen": (
                    {symbol: str(qty)} if side is Side.SELL else {}),
            }

            # 先落盘“已接受+冻结”，再撮合；保证重启不丢已冻结资金。
            self.log.append(EventType.ORDER_ACCEPTED, {
                "order": _order_to_dict(order),
                "frozen": frozen_snapshot,
            })
            self.orders_by_id[order.order_id] = order
            self.idempotency[(account_id, client_order_id)] = order.order_id
            logger.info(
                "订单接受 order=%s client=%s account=%s %s %s qty=%s 判定=风控通过已冻结 状态=NEW",
                order.order_id, client_order_id, account_id, side.value,
                order_type.value, qty)

            self._match(order, freeze_price)

            # 限价单剩余量继续挂簿；市价单剩余量作废并释放冻结。
            if order.remaining_qty > 0:
                if order.order_type is OrderType.LIMIT:
                    self.books[symbol].add(order)
                else:
                    self._expire_market_remainder(order, freeze_price)
            return order

    def _expire_market_remainder(
        self, order: Order, freeze_price: Decimal,
        recovered: bool = False,
    ) -> None:
        """市价单无法成交的剩余量作废（撤单处理），释放对应冻结。"""
        remaining = order.remaining_qty
        account = self.accounts.require(order.account_id)
        self.accounts.release_cancel(
            account, order.symbol, order.side, remaining, freeze_price)
        order.status = OrderStatus.CANCELED
        order.version += 1
        self.log.append(EventType.ORDER_CANCELED, {
            "order_id": order.order_id,
            "remaining_qty": str(remaining),
            "freeze_price": str(freeze_price),
            "reason": "MARKET_REMAINDER_EXPIRED",
            "recovered": recovered,
        })
        logger.info(
            "市价剩余作废 order=%s 剩余=%s 判定=流动性不足%s 最终状态=%s",
            order.order_id, remaining,
            "（崩溃窗口补单）" if recovered else "", order.status.value)

    def _match(self, taker: Order, taker_freeze_price: Decimal,
               recovered: bool = False,
               persisted_fills: set[tuple] | None = None,
               log_recovered: bool = False) -> None:
        book = self.books[taker.symbol]
        replay = persisted_fills is not None
        for maker in book.iter_match(taker):
            if taker.remaining_qty <= 0:
                break
            qty = min(taker.remaining_qty, maker.remaining_qty)
            price = maker.price  # 以挂单价（maker 价）成交
            fill_key = (taker.order_id, maker.order_id,
                        str(price), str(qty))

            if replay and fill_key in persisted_fills:
                # 该成交已落盘：重放只做账务与状态推进，不再重复落盘。
                fill = self._build_fill(
                    taker, maker, qty, price, taker_freeze_price)
                self.accounts.apply_fill(fill)
                self._advance_on_fill(taker, qty, price)
                self._advance_on_fill(maker, qty, price)
                if maker.is_terminal:
                    book.remove(maker)
                continue

            fill = self._build_fill(
                taker, maker, qty, price, taker_freeze_price)
            # 记账（买卖双方冻结扣减/释放原子完成）。
            self.accounts.apply_fill(fill)
            self._advance_on_fill(taker, qty, price)
            self._advance_on_fill(maker, qty, price)

            if maker.is_terminal:
                book.remove(maker)

            # 实时路径必落盘；恢复路径仅崩溃窗口补单（log_recovered）才落盘。
            if replay and not log_recovered:
                continue
            self.log.append(EventType.ORDER_FILLED, {
                "trade_id": fill.trade_id,
                "symbol": fill.symbol,
                "taker_order_id": fill.taker_order_id,
                "maker_order_id": fill.maker_order_id,
                "taker_account": fill.taker_account,
                "maker_account": fill.maker_account,
                "side": fill.side.value,
                "price": str(price),
                "qty": str(qty),
                "ts_ns": fill.ts_ns,
                "taker_freeze_price": str(taker_freeze_price),
                "maker_freeze_price": str(self._freeze_price(maker)),
                "recovered": recovered,
            })
            logger.info(
                "成交 trade=%s taker=%s maker=%s %s qty=%s@%s "
                "判定=价格时间优先%s taker状态=%s maker状态=%s",
                fill.trade_id, taker.order_id, maker.order_id,
                taker.side.value, qty, price,
                "（崩溃窗口补单）" if replay else "",
                taker.status.value, maker.status.value)

    def _build_fill(self, taker: Order, maker: Order, qty: Decimal,
                    price: Decimal, taker_freeze_price: Decimal) -> Fill:
        return Fill(
            trade_id=self._new_trade_id(),
            symbol=taker.symbol,
            taker_order_id=taker.order_id,
            maker_order_id=maker.order_id,
            taker_account=taker.account_id,
            maker_account=maker.account_id,
            side=taker.side,
            price=price,
            qty=qty,
            ts_ns=next(self._order_counter),
            taker_freeze_price=taker_freeze_price,
            maker_freeze_price=self._freeze_price(maker),
        )

    # ---- 撤单 -----------------------------------------------------------
    def cancel_order(self, order_id: str) -> CancelResult:
        with self._lock:
            order = self.orders_by_id.get(order_id)
            if order is None:
                logger.info("撤单 order=%s 判定=订单不存在", order_id)
                return CancelResult(
                    order_id=order_id, canceled=False,
                    status=OrderStatus.REJECTED,
                    reason=CancelFailReason.ORDER_NOT_FOUND.value)

            if order.is_terminal:
                logger.info(
                    "撤单 order=%s 判定=已终态不可撤 最终状态=%s",
                    order_id, order.status.value)
                return CancelResult(
                    order_id=order_id, canceled=False, status=order.status,
                    reason=CancelFailReason.ALREADY_TERMINAL.value)

            remaining = order.remaining_qty
            if remaining <= 0:
                return CancelResult(
                    order_id=order_id, canceled=False, status=order.status,
                    reason=CancelFailReason.NO_REMAINING_QTY.value)

            freeze_price = self._freeze_price(order)
            account = self.accounts.require(order.account_id)
            self.accounts.release_cancel(
                account, order.symbol, order.side, remaining, freeze_price)
            self.books[order.symbol].remove(order)
            order.status = OrderStatus.CANCELED
            order.version += 1

            self.log.append(EventType.ORDER_CANCELED, {
                "order_id": order.order_id,
                "remaining_qty": str(remaining),
                "freeze_price": str(freeze_price),
                "reason": "USER_CANCEL",
            })
            logger.info(
                "撤单成功 order=%s client=%s 释放剩余=%s 判定=用户撤单 最终状态=CANCELED",
                order.order_id, order.client_order_id, remaining)
            return CancelResult(
                order_id=order_id, canceled=True,
                status=OrderStatus.CANCELED, canceled_qty=remaining)

    # ---- 读操作（锁内快照） ---------------------------------------------
    def get_order(self, order_id: str) -> Order | None:
        with self._lock:
            return self.orders_by_id.get(order_id)

    def get_order_by_client_id(
        self, account_id: str, client_order_id: str
    ) -> Order | None:
        with self._lock:
            oid = self.idempotency.get((account_id, client_order_id))
            return self.orders_by_id[oid] if oid else None

    def account_snapshot(self, account_id: str) -> dict | None:
        with self._lock:
            acct = self.accounts.get(account_id)
            return None if acct is None else acct.snapshot()

    def book_snapshot(self, symbol: str) -> dict:
        with self._lock:
            book = self.books.get(symbol)
            if book is None:
                return {"symbol": symbol, "bids": [], "asks": []}
            return book.levels()

    def fills(self) -> list[dict]:
        """从日志提取全部成交（撮合结果查询用）。"""
        with self._lock:
            return [
                e["payload"]
                for e in self.log.read_all()
                if e["type"] == EventType.ORDER_FILLED.value
            ]
