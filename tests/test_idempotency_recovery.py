"""幂等与状态恢复：重复提交不重复成交/扣减；重启后状态与结论一致。"""
import logging

from engine.engine import MatchingEngine
from engine.models import OrderStatus, RejectReason
from tests.conftest import SYMBOL

log = logging.getLogger("matching")


def test_duplicate_submit_no_double_trade(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 5)
    r1 = funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 5)
    assert r1.status is OrderStatus.FILLED and len(r1.trades) == 1
    cash_after_first = funded.snapshot_account("alice")["cash_available"]

    r2 = funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 5)
    assert r2.duplicate and r2.order_id == r1.order_id
    assert r2.status is OrderStatus.FILLED and len(r2.trades) == 1
    # 不重复成交、不重复扣减
    assert len(funded.list_trades()) == 1
    assert funded.snapshot_account("alice")["cash_available"] == cash_after_first
    log.info("duplicate submit: id=%s status=%s trades=%d (unchanged)",
             r2.order_id, r2.status.value, len(r2.trades))


def test_duplicate_rejection_replayed(funded):
    r1 = funded.submit_order("poor", "alice", SYMBOL, "BUY", "LIMIT", 10**9, 1)
    assert r1.reject_reason is RejectReason.INSUFFICIENT_FUNDS
    r2 = funded.submit_order("poor", "alice", SYMBOL, "BUY", "LIMIT", 10**9, 1)
    assert r2.duplicate and r2.reject_reason is RejectReason.INSUFFICIENT_FUNDS


def test_reject_reasons_distinguishable(funded):
    cases = [
        ("bad-qty", dict(price=100, quantity=0), RejectReason.INVALID_ORDER),
        ("bad-symbol", dict(symbol="NOPE", price=100, quantity=1),
         RejectReason.UNKNOWN_SYMBOL),
        ("no-funds", dict(price=10**9, quantity=1), RejectReason.INSUFFICIENT_FUNDS),
    ]
    for key, kw, reason in cases:
        kw.setdefault("symbol", SYMBOL)
        r = funded.submit_order(key, "alice", kw["symbol"], "BUY", "LIMIT",
                                kw["price"], kw["quantity"])
        assert r.reject_reason is reason, (key, r.reject_reason)
        log.info("reject case key=%s reason=%s", key, reason.value)
    r = funded.submit_order("no-pos", "alice", SYMBOL, "SELL", "LIMIT", 100, 1)
    assert r.reject_reason is RejectReason.INSUFFICIENT_POSITION


def _full_state(e: MatchingEngine):
    return {
        "cash": e.total_cash(),
        "pos": e.total_position(SYMBOL),
        "alice": e.snapshot_account("alice"),
        "carol": e.snapshot_account("carol"),
        "orders": {oid: (o.status, o.remaining)
                   for oid, o in ((oid, e.get_order(oid)) for oid in
                                  [f"O{i}" for i in range(1, 20)])
                   if o is not None},
        "trades": [(t.trade_id, t.price, t.quantity) for t in e.list_trades()],
    }


def test_restart_recovery_consistent(tmp_path):
    wal = tmp_path / "wal.jsonl"
    e1 = MatchingEngine([SYMBOL], wal)
    e1.deposit_cash("alice", 1_000_000)
    e1.deposit_position("carol", SYMBOL, 1_000)
    e1.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 10)
    e1.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 600, 4)   # 部分成交
    e1.submit_order("b2", "alice", SYMBOL, "BUY", "LIMIT", 400, 3)   # 纯挂单
    e1.submit_order("bad", "alice", SYMBOL, "BUY", "LIMIT", 10**9, 1)  # 拒单
    r_cancel = e1.submit_order("b3", "alice", SYMBOL, "BUY", "LIMIT", 300, 2)
    e1.cancel_order(r_cancel.order_id)                                # 撤单
    before = _full_state(e1)

    # 模拟进程重启：新实例从同一 WAL 恢复
    e2 = MatchingEngine([SYMBOL], wal)
    after = _full_state(e2)
    assert before == after
    log.info("recovery: orders=%s trades=%s consistent=True",
             after["orders"], after["trades"])

    # 恢复后幂等仍生效：重复键不重复执行
    rdup = e2.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 600, 4)
    assert rdup.duplicate and len(e2.list_trades()) == len(before["trades"])
    rrej = e2.submit_order("bad", "alice", SYMBOL, "BUY", "LIMIT", 10**9, 1)
    assert rrej.duplicate and rrej.reject_reason is RejectReason.INSUFFICIENT_FUNDS

    # 恢复后可继续撮合，序号不冲突
    r = e2.submit_order("s2", "carol", SYMBOL, "SELL", "LIMIT", 400, 3)
    assert r.status is OrderStatus.FILLED  # 吃掉重启前挂的 b2@400
    assert e2.total_cash() == 1_000_000
    assert e2.total_position(SYMBOL) == 1_000
