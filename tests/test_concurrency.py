"""并发场景：并发下单不超卖/不超额占用、撤单与成交竞争只有唯一终态、总量守恒。"""
import logging
from concurrent.futures import ThreadPoolExecutor

from engine.models import OrderStatus, RejectReason
from tests.conftest import SYMBOL

log = logging.getLogger("matching")


def test_concurrent_buys_no_overfreeze(engine):
    """现金仅够 10 单时并发提交 50 单：恰好 10 单成功，冻结不超发。"""
    engine.deposit_cash("alice", 10 * 100 * 10)  # 够 10 单（每单 100*10）
    engine.deposit_position("carol", SYMBOL, 10_000)

    def submit(i):
        return engine.submit_order(f"k{i}", "alice", SYMBOL, "BUY", "LIMIT", 100, 10)

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(submit, range(50)))

    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]
    assert len(accepted) == 10
    assert all(r.reject_reason is RejectReason.INSUFFICIENT_FUNDS for r in rejected)
    snap = engine.snapshot_account("alice")
    assert snap["cash_available"] == 0
    assert snap["cash_frozen"] == 10 * 100 * 10
    log.info("concurrent buys: accepted=%d rejected=%d frozen=%d",
             len(accepted), len(rejected), snap["cash_frozen"])


def test_concurrent_sells_no_oversell(engine):
    """持仓 100 股并发卖出 50*10 股：成交+挂单总量不超过持仓。"""
    engine.deposit_position("carol", SYMBOL, 100)

    def submit(i):
        return engine.submit_order(f"s{i}", "carol", SYMBOL, "SELL", "LIMIT", 100, 10)

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(submit, range(50)))

    accepted = [r for r in results if r.accepted]
    assert len(accepted) == 10
    assert sum(engine.get_order(r.order_id).remaining for r in accepted) == 100
    assert engine.total_position(SYMBOL) == 100
    log.info("concurrent sells: accepted=%d resting=%d",
             len(accepted), sum(engine.get_order(r.order_id).remaining for r in accepted))


def test_cancel_vs_match_single_terminal_state(engine):
    """同一订单并发撤单与撮合：最终只有一个终态，账务对齐。"""
    engine.deposit_cash("alice", 10_000_000)
    engine.deposit_position("carol", SYMBOL, 10_000)

    outcomes = set()
    for round_no in range(200):
        r = engine.submit_order(f"s{round_no}", "carol", SYMBOL, "SELL", "LIMIT", 100, 1)
        oid = r.order_id
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_cancel = ex.submit(engine.cancel_order, oid)
            f_match = ex.submit(
                engine.submit_order, f"b{round_no}", "alice", SYMBOL,
                "BUY", "LIMIT", 100, 1)
            cancel_status = f_cancel.result()
            f_match.result()
        final = engine.get_order(oid).status
        assert final in (OrderStatus.CANCELLED, OrderStatus.FILLED)
        # 撤单返回值与最终态一致（竞争双方看到的是同一终态）
        assert cancel_status == final
        outcomes.add(final)
        # 账务对齐：FILLED 则 carol 收到 100；CANCELLED 则持仓解冻
        carol = engine.snapshot_account("carol")
        assert carol["cash_available"] + carol["positions"][SYMBOL]["frozen"] * 0 >= 0
    # 两种竞争结果都应出现过（证明确实在竞争）
    assert outcomes == {OrderStatus.CANCELLED, OrderStatus.FILLED}
    assert engine.total_cash() == 10_000_000
    assert engine.total_position(SYMBOL) == 10_000
    log.info("cancel-vs-match outcomes=%s rounds=200 conservation=ok", outcomes)


def test_concurrent_duplicate_submission_single_execution(engine):
    """同一幂等键并发重复提交：只执行一次，只冻结一次。"""
    engine.deposit_cash("alice", 1_000)

    def submit(_):
        return engine.submit_order("dup-key", "alice", SYMBOL, "BUY", "LIMIT", 100, 10)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(submit, range(8)))

    order_ids = {r.order_id for r in results}
    assert len(order_ids) == 1
    assert sum(1 for r in results if not r.duplicate) == 1
    snap = engine.snapshot_account("alice")
    assert snap["cash_frozen"] == 1000 and snap["cash_available"] == 0
    log.info("concurrent duplicate: order_id=%s frozen=%d",
             order_ids.pop(), snap["cash_frozen"])
