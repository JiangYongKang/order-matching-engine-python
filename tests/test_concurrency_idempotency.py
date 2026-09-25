"""并发下单、撤单/成交竞争、幂等。"""

import logging
import threading
from decimal import Decimal as D

from app.enums import OrderStatus, Side
from conftest import place

log = logging.getLogger("app.test.concurrent")


def test_concurrent_buys_never_oversell(engine):
    """大量并发买单争抢一个卖单：总成交量不得超过卖单量，账务守恒。"""
    sell = place(engine, "s", "SELLER", Side.SELL, "50", "100")
    results = []
    errors = []

    def worker(i):
        try:
            o = place(engine, f"b{i}", "RICH", Side.BUY, "1", "100")
            results.append(o)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    filled = [o for o in results if o.status is OrderStatus.FILLED]
    total_filled = sum((o.filled_qty for o in results), D(0))
    log.info("卖单=%s 判定=200并发买单争抢50量 成交订单数=%s 总成交=%s 最终状态=%s",
             sell.order_id, len(filled), total_filled, sell.status.value)
    assert total_filled == D("50"), total_filled          # 不超卖
    assert sell.status is OrderStatus.FILLED
    assert sell.filled_qty == D("50")

    seller = engine.account_snapshot("SELLER")
    assert seller["position_frozen"].get("AAA", D(0)) == D("0")
    # 卖方持仓：100 - 50 = 50；收到现金 5000
    assert seller["position_total"]["AAA"] == D("50")
    assert seller["cash_total"] == D("6000")


def test_concurrent_sells_no_overspend_cash(engine):
    """并发卖单争抢一个买单：买方资金支出不超过其冻结额。"""
    buy = place(engine, "b", "BUYER", Side.BUY, "30", "100")
    orders = []

    def worker(i):
        orders.append(
            place(engine, f"s{i}", "RICH", Side.SELL, "1", "100"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = sum((o.filled_qty for o in orders), D(0))
    log.info("买单=%s 判定=100并发卖单争抢30量 总成交=%s 最终状态=%s",
             buy.order_id, total, buy.status.value)
    assert total == D("30")
    assert buy.status is OrderStatus.FILLED
    buyer = engine.account_snapshot("BUYER")
    assert buyer["cash_frozen"] == D("0")
    assert buyer["cash_total"] == D("7000")   # 10000-3000
    assert buyer["position_total"]["AAA"] == D("30")


def test_cancel_vs_fill_race_single_terminal_state(engine):
    """同一订单上并发撤单与吃单：FILLED 与 CANCELED 只能出现一个，
    且 成交量 + 撤销量 == 原始委托量（冻结不丢失、不重复）。"""
    outcomes = {"canceled": 0, "filled": 0}
    lock = threading.Lock()

    def trial(i):
        # 每组用唯一价位，避免上一组遗留挂单与本组交叉撮合。
        px = 100 + i
        sell = place(engine, f"s{i}", "RICH", Side.SELL, "5", str(px))
        canceled_qty = {"v": None}

        def cancel():
            r = engine.cancel_order(sell.order_id)
            if r.canceled:
                canceled_qty["v"] = r.canceled_qty
                with lock:
                    outcomes["canceled"] += 1

        def eat():
            o = place(engine, f"e{i}", "BUYER", Side.BUY, "5", str(px))
            with lock:
                if o.status is OrderStatus.FILLED:
                    outcomes["filled"] += 1

        t1 = threading.Thread(target=cancel)
        t2 = threading.Thread(target=eat)
        t1.start(); t2.start()
        t1.join(); t2.join()

        final = engine.get_order(sell.order_id)
        log.info(
            "竞争订单=%s 判定=撤单与成交临界区互斥 最终状态=%s filled=%s canceled=%s",
            sell.order_id, final.status.value, final.filled_qty,
            canceled_qty["v"])
        # 关键不变式：终态唯一
        assert final.status in (OrderStatus.CANCELED, OrderStatus.FILLED)
        # 成交量 + 撤销量 == 原始量，账务不重不漏
        canceled = canceled_qty["v"] or D(0)
        assert final.filled_qty + canceled == D("5"), (
            final.filled_qty, canceled)

    # 12 组竞争顺序执行（每组独立订单、独立价位）。
    for i in range(12):
        trial(i)

    assert outcomes["canceled"] + outcomes["filled"] >= 1
    log.info("竞争汇总 撤单成功=%s 对手全额成交=%s",
             outcomes["canceled"], outcomes["filled"])


def test_idempotent_duplicate_submit(engine):
    o1 = place(engine, "dup-1", "BUYER", Side.BUY, "3", "100")
    o2 = place(engine, "dup-1", "BUYER", Side.BUY, "3", "100")  # 同键
    o3 = place(engine, "dup-1", "BUYER", Side.BUY, "9", "100")  # 参数不同也忽略
    log.info("幂等键=dup-1 判定=重复提交返回原单 最终状态=%s order=%s",
             o1.status.value, o1.order_id)
    assert o1.order_id == o2.order_id == o3.order_id
    # 只冻结一次：买单 3@100=300；若无对手则全部挂单冻结
    buyer = engine.account_snapshot("BUYER")
    assert buyer["cash_frozen"] == D("300"), buyer["cash_frozen"]


def test_concurrent_same_idempotency_key_single_order(engine):
    orders = []
    lock = threading.Lock()

    def worker():
        o = place(engine, "same-key", "RICH", Side.SELL, "5", "100")
        with lock:
            orders.append(o)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = {o.order_id for o in orders}
    log.info("并发幂等键=same-key 判定=只接受一笔 唯一订单数=%s", len(ids))
    assert ids == {orders[0].order_id}
    assert len(ids) == 1
    # 只冻结一份持仓
    rich = engine.account_snapshot("RICH")
    assert rich["position_frozen"]["AAA"] == D("5")
