"""撮合规则：价格优先/时间优先、部分成交、剩余挂单、市价单。"""

import logging
from decimal import Decimal as D

from app.enums import OrderStatus, OrderType, Side
from conftest import place

log = logging.getLogger("app.test.matching")


def test_price_time_priority(engine):
    """同价时间优先；更优价位先成交。"""
    # 卖：100 两张（s_early 更早）、99 一张
    s_early = place(engine, "se", "SELLER", Side.SELL, "2", "100")
    s_late = place(engine, "sl", "RICH", Side.SELL, "2", "100")
    s_99 = place(engine, "s99", "RICH", Side.SELL, "3", "99")

    taker = place(engine, "t", "BUYER", Side.BUY, "7", "100")
    fills = engine.fills()
    seq = [(f["maker_order_id"], f["qty"]) for f in fills]
    log.info("订单=%s 判定=价格优先再时间优先 成交序列=%s 最终状态=%s",
             taker.order_id, seq, taker.status.value)
    assert [m for m, _ in seq] == [s_99.order_id, s_early.order_id,
                                   s_late.order_id]
    assert taker.status is OrderStatus.FILLED
    assert sum((D(q) for _, q in seq), D(0)) == D("7")


def test_partial_fill_and_rest_booked(engine):
    """允许部分成交，限价单剩余量继续挂单。"""
    sell = place(engine, "s", "SELLER", Side.SELL, "10", "100")
    buy = place(engine, "b", "BUYER", Side.BUY, "4", "100")

    log.info("订单=%s 判定=对手量不足部分成交 已成交=%s 剩余=%s 最终状态=%s",
             buy.order_id, buy.filled_qty, buy.remaining_qty,
             buy.status.value)
    assert buy.status is OrderStatus.FILLED and buy.filled_qty == D("4")
    assert sell.status is OrderStatus.PARTIALLY_FILLED
    assert sell.filled_qty == D("4") and sell.remaining_qty == D("6")

    book = engine.book_snapshot("AAA")
    assert book["asks"][0]["price"] == D("100")
    assert book["asks"][0]["remaining_qty"] == D("6")


def test_unmatched_limit_rests_and_matches_later(engine):
    """无对手价时挂簿，后续来单再撮合。"""
    buy = place(engine, "b", "BUYER", Side.BUY, "5", "90")
    assert buy.status is OrderStatus.NEW
    sell = place(engine, "s", "SELLER", Side.SELL, "5", "90")
    log.info("订单=%s 判定=与先前买一交叉 最终状态=%s",
             sell.order_id, sell.status.value)
    assert sell.status is OrderStatus.FILLED
    assert engine.get_order(buy.order_id).status is OrderStatus.FILLED


def test_market_buy_respects_protection_price(engine):
    place(engine, "a1", "RICH", Side.SELL, "2", "99")
    place(engine, "a2", "RICH", Side.SELL, "3", "101")
    # 保护价 100：只能吃 99 的 2 张，剩余作废
    m = place(engine, "m", "BUYER", Side.BUY, "5", None,
              otype=OrderType.MARKET, max_price="100")
    log.info("订单=%s 判定=市价买保护价100 成交=%s 最终状态=%s",
             m.order_id, m.filled_qty, m.status.value)
    assert m.filled_qty == D("2")
    assert m.status is OrderStatus.CANCELED  # 剩余量作废


def test_market_sell_consumes_best_bid(engine):
    place(engine, "d1", "RICH", Side.BUY, "4", "90")
    m = place(engine, "ms", "SELLER", Side.SELL, "6", None,
              otype=OrderType.MARKET)
    log.info("订单=%s 判定=市价卖吃买一 成交=%s 最终状态=%s",
             m.order_id, m.filled_qty, m.status.value)
    assert m.filled_qty == D("4")
    assert m.status is OrderStatus.CANCELED


def test_terminal_order_never_matches_again(engine):
    sell = place(engine, "s", "SELLER", Side.SELL, "2", "100")
    place(engine, "b1", "BUYER", Side.BUY, "2", "100")  # 打满卖单
    assert sell.status is OrderStatus.FILLED
    # 再来买单不应与已终止卖单成交
    b2 = place(engine, "b2", "BUYER", Side.BUY, "2", "100")
    log.info("订单=%s 判定=对手已终态不参与 最终状态=%s",
             b2.order_id, b2.status.value)
    assert b2.status is OrderStatus.NEW  # 挂在买簿
    assert len(engine.fills()) == 1
