"""风控拒单、账务冻结/释放、总量守恒。"""

import logging
from decimal import Decimal as D

import pytest

from app.enums import OrderRejected, OrderStatus, OrderType, RejectReason, Side
from conftest import place

log = logging.getLogger("app.test.risk")


def _conservation(engine, cash_total, positions_total):
    cash = sum(
        engine.account_snapshot(a)["cash_total"]
        for a in ("BUYER", "SELLER", "RICH"))
    assert cash == cash_total, cash
    for sym, total in positions_total.items():
        pos = sum(
            engine.account_snapshot(a)["position_total"].get(sym, D(0))
            for a in ("BUYER", "SELLER", "RICH"))
        assert pos == total, (sym, pos)


def test_reject_reasons_are_distinguishable(engine):
    cases = [
        dict(client="r1", account="GHOST", symbol="AAA", side=Side.BUY,
             qty="1", price="100", reason=RejectReason.UNKNOWN_ACCOUNT),
        dict(client="r2", account="BUYER", symbol="XYZ", side=Side.BUY,
             qty="1", price="100", reason=RejectReason.UNKNOWN_SYMBOL),
        dict(client="r3", account="BUYER", symbol="AAA", side=Side.BUY,
             qty="1", price="0", reason=RejectReason.INVALID_PRICE),
        dict(client="r4", account="BUYER", symbol="AAA", side=Side.BUY,
             qty="0", price="100", reason=RejectReason.INVALID_QTY),
        dict(client="r5", account="BUYER", symbol="AAA", side=Side.BUY,
             qty="1", price=None, otype=OrderType.MARKET,
             reason=RejectReason.MARKET_BUY_NEEDS_PROTECTION),
        dict(client="r6", account="BUYER", symbol="AAA", side=Side.BUY,
             qty="999999", price="100",
             reason=RejectReason.INSUFFICIENT_AVAILABLE_CASH),
        dict(client="r7", account="BUYER", symbol="AAA", side=Side.SELL,
             qty="999999", price="100",
             reason=RejectReason.INSUFFICIENT_AVAILABLE_POSITION),
    ]
    for c in cases:
        reason = c.pop("reason")
        with pytest.raises(OrderRejected) as ei:
            place(engine, **c)
        log.info("拒单 client=%s 判定=%s 依据=%s 最终状态=REJECTED",
                 c["client"], ei.value.reason.value, ei.value.detail)
        assert ei.value.reason is reason
        # 拒单不产生订单、不冻结
        assert engine.get_order_by_client_id(c["account"], c["client"]) is None
    _conservation(engine, D("1011000"), {"AAA": D("100100"), "BBB": D("0")})


def test_freeze_on_submit_release_on_cancel(engine):
    before = engine.account_snapshot("BUYER")
    o = place(engine, "b", "BUYER", Side.BUY, "10", "100")  # 冻结 1000
    mid = engine.account_snapshot("BUYER")
    log.info("订单=%s 判定=下单冻结资金1000 可用=%s 冻结=%s",
             o.order_id, mid["cash_available"], mid["cash_frozen"])
    assert mid["cash_available"] == before["cash_available"] - D("1000")
    assert mid["cash_frozen"] == D("1000")

    engine.cancel_order(o.order_id)
    after = engine.account_snapshot("BUYER")
    log.info("订单=%s 判定=撤单释放冻结 最终状态=%s 可用=%s 冻结=%s",
             o.order_id, o.status.value, after["cash_available"],
             after["cash_frozen"])
    assert after["cash_available"] == before["cash_available"]
    assert after["cash_frozen"] == D("0")


def test_fill_accounting_conservation_with_price_improvement(engine):
    """买方按更高限价冻结、以更低价成交，价差须退回，现金守恒。"""
    sell = place(engine, "s", "SELLER", Side.SELL, "10", "90")
    buy = place(engine, "b", "BUYER", Side.BUY, "10", "100")
    assert buy.status is OrderStatus.FILLED
    assert sell.status is OrderStatus.FILLED and sell.avg_fill_price == D("90")
    b = engine.account_snapshot("BUYER")
    s = engine.account_snapshot("SELLER")
    log.info("订单=%s 判定=以90成交、100冻结价差退回 买方可用=%s 冻结=%s 持仓=%s",
             buy.order_id, b["cash_available"], b["cash_frozen"],
             b["position_total"]["AAA"])
    # 买方：花 900，冻结清零，持券 10
    assert b["cash_total"] == D("9100") and b["cash_frozen"] == D("0")
    assert b["position_total"]["AAA"] == D("10")
    assert s["cash_total"] == D("1900")
    assert s["position_total"]["AAA"] == D("90")
    _conservation(engine, D("1011000"), {"AAA": D("100100"), "BBB": D("0")})
