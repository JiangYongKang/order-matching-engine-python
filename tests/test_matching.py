"""撮合规则：价格优先、时间优先、部分成交、市价 IOC、终态不再撮合。"""
from engine.models import OrderStatus, RejectReason
from tests.conftest import SYMBOL


def test_price_priority(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 5)
    funded.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 400, 5)
    r = funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 600, 5)
    # 价格优先：应先吃 400 的卖单
    assert r.trades[0].price == 400 and r.trades[0].sell_order_id == "O2"
    assert funded.get_order("O1").remaining == 5


def test_time_priority_same_price(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    funded.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 500, 3)
    r = funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 4)
    assert [t.sell_order_id for t in r.trades] == ["O1", "O2"]
    assert r.trades[0].quantity == 3 and r.trades[1].quantity == 1
    assert funded.get_order("O2").status is OrderStatus.PARTIALLY_FILLED


def test_partial_fill_and_resting(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 2)
    r = funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 10)
    assert r.status is OrderStatus.PARTIALLY_FILLED
    o = funded.get_order(r.order_id)
    assert o.remaining == 8
    # 剩余量继续挂单，可被后续卖单撮合
    r2 = funded.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 500, 8)
    assert r2.status is OrderStatus.FILLED
    assert funded.get_order(r.order_id).status is OrderStatus.FILLED


def test_trade_price_is_maker_price(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 400, 5)
    r = funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 600, 5)
    assert r.trades[0].price == 400
    # 买方按 600 冻结、按 400 成交，差额退回
    snap = funded.snapshot_account("alice")
    assert snap["cash_available"] == 1_000_000 - 5 * 400
    assert snap["cash_frozen"] == 0


def test_market_order_ioc_remainder_cancelled(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    r = funded.submit_order("m1", "alice", SYMBOL, "BUY", "MARKET", 0, 10)
    o = funded.get_order(r.order_id)
    assert sum(t.quantity for t in r.trades) == 3
    assert o.status is OrderStatus.CANCELLED and o.remaining == 7
    # 冻结全部释放
    assert funded.snapshot_account("alice")["cash_frozen"] == 0


def test_market_order_no_liquidity_rejected(funded):
    r = funded.submit_order("m1", "alice", SYMBOL, "BUY", "MARKET", 0, 1)
    assert not r.accepted and r.reject_reason is RejectReason.NO_LIQUIDITY


def test_terminal_order_not_matched_again(funded):
    r1 = funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 5)
    funded.cancel_order(r1.order_id)
    assert funded.get_order(r1.order_id).status is OrderStatus.CANCELLED
    # 已撤销订单不得再参与撮合
    r2 = funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 5)
    assert r2.trades == [] and r2.status is OrderStatus.ACCEPTED


def test_conservation_after_trades(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 10)
    funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 600, 4)
    funded.submit_order("b2", "bob", SYMBOL, "BUY", "LIMIT", 500, 6)
    assert funded.total_cash() == 2_000_000
    assert funded.total_position(SYMBOL) == 2_000
