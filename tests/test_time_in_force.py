"""成交方式（GTC/IOC/FOK）与市价单价格保护：语义、原子性、幂等、恢复、兼容。"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from engine.engine import MatchingEngine
from engine.models import OrderStatus, RejectReason, TimeInForce
from tests.conftest import SYMBOL

log = logging.getLogger("matching")


def _mk():
    e = MatchingEngine([SYMBOL])
    e.deposit_cash("alice", 1_000_000)
    e.deposit_cash("bob", 1_000_000)
    e.deposit_position("carol", SYMBOL, 1_000)
    e.deposit_position("dave", SYMBOL, 1_000)
    return e


# ---------- FOK：立即全部成交，否则整单不成交 ----------

def test_fok_limit_fully_filled(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 5)
    r = funded.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 600, 5,
                            time_in_force="FOK")
    o = funded.get_order(r.order_id)
    assert r.status is OrderStatus.FILLED and o.time_in_force is TimeInForce.FOK
    assert sum(t.quantity for t in r.trades) == 5
    log.info("fok ok: id=%s basis=fillable>=qty status=%s", r.order_id, r.status.value)


def test_fok_insufficient_liquidity_atomic_failure(funded):
    """流动性不足：整单失败，无半成交、无残留冻结、账务不变、对手单不受影响。"""
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    before_alice = funded.snapshot_account("alice")
    before_carol = funded.snapshot_account("carol")
    before_cash = funded.total_cash()

    r = funded.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 600, 10,
                            time_in_force="FOK")
    assert not r.accepted and r.status is OrderStatus.REJECTED
    assert r.reject_reason is RejectReason.FOK_UNFILLABLE
    assert r.trades == []
    o = funded.get_order(r.order_id)
    assert o.status is OrderStatus.REJECTED and o.remaining == 10
    assert o.reject_reason is RejectReason.FOK_UNFILLABLE
    # 账务逐项一致：无冻结残留、无成交、总量守恒
    assert funded.snapshot_account("alice") == before_alice
    assert funded.snapshot_account("carol") == before_carol
    assert funded.total_cash() == before_cash
    assert funded.list_trades() == []
    # 卖单未被吃掉，仍挂盘中
    assert funded.get_order("O1").remaining == 3
    log.info("fok atomic-fail: id=%s basis=fillable<qty status=REJECTED "
             "reason=FOK_UNFILLABLE ledger_unchanged=True", r.order_id)


def test_fok_market_respects_protection_cap(funded):
    """FOK 市价买：保护价之内可成交量不足 -> 整单失败。"""
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    funded.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 900, 10)
    r = funded.submit_order("f1", "alice", SYMBOL, "BUY", "MARKET", 0, 5,
                            time_in_force="FOK", protection_price=600)
    assert r.status is OrderStatus.REJECTED
    assert r.reject_reason is RejectReason.FOK_UNFILLABLE
    assert funded.snapshot_account("alice")["cash_frozen"] == 0
    assert funded.list_trades() == []
    log.info("fok market cap: id=%s basis=fillable_within_cap<qty status=REJECTED",
             r.order_id)


def test_fok_sell_side(funded):
    funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 4)
    r = funded.submit_order("f1", "carol", SYMBOL, "SELL", "LIMIT", 400, 4,
                            time_in_force="FOK")
    assert r.status is OrderStatus.FILLED
    r2 = funded.submit_order("f2", "dave", SYMBOL, "SELL", "LIMIT", 400, 1,
                             time_in_force="FOK")
    assert r2.status is OrderStatus.REJECTED
    assert r2.reject_reason is RejectReason.FOK_UNFILLABLE
    # 卖单 FOK 失败：持仓冻结全额释放
    assert funded.snapshot_account("dave")["positions"][SYMBOL]["frozen"] == 0


def test_fok_failure_idempotent_replay(funded):
    """FOK 失败后同一幂等键重提：回放同一拒绝结论，不重新执行。"""
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 1)
    r1 = funded.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 600, 10,
                             time_in_force="FOK")
    r2 = funded.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 600, 10,
                             time_in_force="FOK")
    assert r2.duplicate and r2.order_id == r1.order_id
    assert not r2.accepted and r2.reject_reason is RejectReason.FOK_UNFILLABLE
    assert funded.snapshot_account("alice")["cash_frozen"] == 0
    log.info("fok dup: id=%s status=REJECTED replayed=True", r2.order_id)


# ---------- IOC：立即成交，剩余撤销 ----------

def test_ioc_limit_partial_then_cancel(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    r = funded.submit_order("i1", "alice", SYMBOL, "BUY", "LIMIT", 600, 10,
                            time_in_force="IOC")
    o = funded.get_order(r.order_id)
    assert sum(t.quantity for t in r.trades) == 3
    assert o.status is OrderStatus.CANCELLED and o.remaining == 7
    assert funded.snapshot_account("alice")["cash_frozen"] == 0
    log.info("ioc: id=%s filled=3 remainder=7 decision=CANCELLED status=%s",
             r.order_id, o.status.value)


def test_default_tif_unchanged(funded):
    """不传 time_in_force：限价单挂盘（GTC），市价单 IOC，与旧行为一致。"""
    r = funded.submit_order("g1", "alice", SYMBOL, "BUY", "LIMIT", 100, 2)
    o = funded.get_order(r.order_id)
    assert o.status is OrderStatus.ACCEPTED and o.time_in_force is TimeInForce.GTC

    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 1)
    r2 = funded.submit_order("m1", "bob", SYMBOL, "BUY", "MARKET", 0, 5)
    o2 = funded.get_order(r2.order_id)
    assert o2.status is OrderStatus.CANCELLED and o2.remaining == 4
    assert o2.time_in_force is TimeInForce.IOC


def test_invalid_tif_and_protection_params(funded):
    r = funded.submit_order("x1", "alice", SYMBOL, "BUY", "MARKET", 0, 1,
                            time_in_force="GTC")
    assert r.reject_reason is RejectReason.INVALID_ORDER
    r = funded.submit_order("x2", "alice", SYMBOL, "BUY", "LIMIT", 100, 1,
                            protection_price=100)
    assert r.reject_reason is RejectReason.INVALID_ORDER
    r = funded.submit_order("x3", "alice", SYMBOL, "BUY", "LIMIT", 100, 1,
                            time_in_force="DAY")
    assert r.reject_reason is RejectReason.INVALID_ORDER


# ---------- 市价单价格保护 ----------

def test_market_buy_protection_cap_stops_above(funded):
    """市价买带上限：只吃上限之内的档位，超出即停，剩余撤销，花费不超冻结。"""
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    funded.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 700, 3)
    r = funded.submit_order("m1", "alice", SYMBOL, "BUY", "MARKET", 0, 5,
                            protection_price=600)
    o = funded.get_order(r.order_id)
    assert [t.price for t in r.trades] == [500]  # 700 档位触上限，未成交
    assert o.status is OrderStatus.CANCELLED and o.remaining == 2
    assert o.protection_price == 600
    snap = funded.snapshot_account("alice")
    assert snap["cash_frozen"] == 0
    assert snap["cash_available"] == 1_000_000 - 3 * 500  # 花费 <= 冻结 600*5
    assert funded.total_cash() == 2_000_000
    log.info("mkt buy cap: id=%s cap=600 hit=True filled=3 remainder=2 "
             "decision=CANCELLED", r.order_id)


def test_market_buy_default_protection_is_best_ask(funded):
    """不带保护价：默认口径=下单时最优卖一价，不吃更高档，绝不透支冻结额。"""
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 2)
    funded.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 800, 2)
    r = funded.submit_order("m1", "alice", SYMBOL, "BUY", "MARKET", 0, 4)
    o = funded.get_order(r.order_id)
    assert [t.price for t in r.trades] == [500]  # 默认保护=500，800 档不吃
    assert o.protection_price == 500
    assert o.status is OrderStatus.CANCELLED and o.remaining == 2
    snap = funded.snapshot_account("alice")
    assert snap["cash_available"] == 1_000_000 - 2 * 500  # 未透支
    assert snap["cash_frozen"] == 0
    log.info("mkt buy default-cap: id=%s cap=500(default best_ask) hit=True",
             r.order_id)


def test_market_sell_protection_floor(funded):
    """市价卖带下限：低于下限的买单不吃，剩余撤销并释放持仓冻结。"""
    funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 3)
    funded.submit_order("b2", "bob", SYMBOL, "BUY", "LIMIT", 300, 3)
    r = funded.submit_order("m1", "carol", SYMBOL, "SELL", "MARKET", 0, 5,
                            protection_price=400)
    o = funded.get_order(r.order_id)
    assert [t.price for t in r.trades] == [500]  # 300 档低于下限，未成交
    assert o.status is OrderStatus.CANCELLED and o.remaining == 2
    pos = funded.snapshot_account("carol")["positions"][SYMBOL]
    assert pos["frozen"] == 0 and pos["available"] == 1_000 - 3
    assert funded.total_position(SYMBOL) == 2_000
    log.info("mkt sell floor: id=%s floor=400 hit=True filled=3 remainder=2",
             r.order_id)


def test_market_sell_default_floor_is_best_bid(funded):
    """市价卖不带保护价：默认下限=下单时最优买一价。"""
    funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 2)
    funded.submit_order("b2", "bob", SYMBOL, "BUY", "LIMIT", 300, 2)
    r = funded.submit_order("m1", "carol", SYMBOL, "SELL", "MARKET", 0, 4)
    o = funded.get_order(r.order_id)
    assert [t.price for t in r.trades] == [500]
    assert o.protection_price == 500 and o.remaining == 2


def test_market_buy_cost_never_exceeds_locked(funded):
    """多档成交：成交总额 <= 下单时锁定额度，差额退回，总量守恒。"""
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 400, 2)
    funded.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 550, 2)
    r = funded.submit_order("m1", "alice", SYMBOL, "BUY", "MARKET", 0, 4,
                            protection_price=600)
    cost = sum(t.price * t.quantity for t in r.trades)
    assert cost == 400 * 2 + 550 * 2
    assert cost <= 600 * 4  # 不超过锁定额度
    snap = funded.snapshot_account("alice")
    assert snap["cash_available"] == 1_000_000 - cost
    assert snap["cash_frozen"] == 0
    assert funded.total_cash() == 2_000_000
    assert funded.total_position(SYMBOL) == 2_000


# ---------- 恢复与兼容 ----------

def _full_state(e: MatchingEngine):
    return {
        "cash": e.total_cash(),
        "pos": e.total_position(SYMBOL),
        "alice": e.snapshot_account("alice"),
        "carol": e.snapshot_account("carol"),
        "orders": {oid: (o.status, o.remaining, o.time_in_force,
                         o.protection_price, o.reject_reason)
                   for oid in (f"O{i}" for i in range(1, 30))
                   if (o := e.get_order(oid)) is not None},
        "trades": [(t.trade_id, t.price, t.quantity) for t in e.list_trades()],
    }


def test_restart_recovery_with_new_fields(tmp_path):
    """FOK 失败/成功、IOC、价格保护订单：重启后状态逐项一致，幂等仍生效。"""
    wal = tmp_path / "wal.jsonl"
    e1 = MatchingEngine([SYMBOL], wal)
    e1.deposit_cash("alice", 1_000_000)
    e1.deposit_position("carol", SYMBOL, 1_000)
    e1.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    e1.submit_order("s2", "carol", SYMBOL, "SELL", "LIMIT", 900, 10)
    e1.submit_order("fok-bad", "alice", SYMBOL, "BUY", "LIMIT", 600, 50,
                    time_in_force="FOK")                       # 整单失败
    e1.submit_order("fok-ok", "alice", SYMBOL, "BUY", "LIMIT", 600, 3,
                    time_in_force="FOK")                       # 全成
    e1.submit_order("ioc", "alice", SYMBOL, "BUY", "MARKET", 0, 5,
                    protection_price=950)                      # 保护价内部分成交
    e1.submit_order("mkt", "alice", SYMBOL, "BUY", "MARKET", 0, 2)  # 默认口径
    before = _full_state(e1)

    e2 = MatchingEngine([SYMBOL], wal)
    after = _full_state(e2)
    assert before == after
    log.info("recovery(new fields): orders=%s consistent=True",
             sorted(after["orders"]))

    # 恢复后幂等重放 FOK 失败结论
    r = e2.submit_order("fok-bad", "alice", SYMBOL, "BUY", "LIMIT", 600, 50,
                        time_in_force="FOK")
    assert r.duplicate and r.reject_reason is RejectReason.FOK_UNFILLABLE
    assert _full_state(e2) == after  # 重提不改变任何状态


def test_old_wal_without_new_fields_replays(tmp_path):
    """旧格式 WAL（无 time_in_force/protection_price/order_id）按原语义读入。"""
    wal = tmp_path / "wal.jsonl"
    events = [
        {"type": "DEPOSIT_CASH", "account_id": "alice", "amount": 1_000_000},
        {"type": "DEPOSIT_POSITION", "account_id": "carol", "symbol": SYMBOL,
         "qty": 100},
        {"type": "ORDER_ACCEPTED", "order_id": "O1", "account_id": "carol",
         "symbol": SYMBOL, "side": "SELL", "order_type": "LIMIT", "price": 500,
         "quantity": 10, "frozen_price": 0, "idempotency_key": "s1", "seq": 1},
        {"type": "ORDER_ACCEPTED", "order_id": "O2", "account_id": "alice",
         "symbol": SYMBOL, "side": "BUY", "order_type": "LIMIT", "price": 600,
         "quantity": 4, "frozen_price": 600, "idempotency_key": "b1", "seq": 2},
        {"type": "TRADE", "trade_id": "T3", "s": SYMBOL, "p": 500, "q": 4,
         "buy_order_id": "O2", "sell_order_id": "O1",
         "buy_account_id": "alice", "sell_account_id": "carol", "seq": 3},
        {"type": "ORDER_REJECTED", "idempotency_key": "bad",
         "account_id": "alice", "symbol": SYMBOL,
         "reason": "INSUFFICIENT_FUNDS"},
    ]
    with open(wal, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in events)

    eng = MatchingEngine([SYMBOL], wal)
    o1, o2 = eng.get_order("O1"), eng.get_order("O2")
    assert o1.status is OrderStatus.PARTIALLY_FILLED and o1.remaining == 6
    assert o2.status is OrderStatus.FILLED
    # 旧订单按默认口径解析：限价=GTC、保护价 0，结论不变
    assert o1.time_in_force is TimeInForce.GTC and o1.protection_price == 0
    assert o2.time_in_force is TimeInForce.GTC
    assert eng.total_cash() == 1_000_000 and eng.total_position(SYMBOL) == 100
    # 旧拒绝记录的幂等语义不变
    r = eng.submit_order("bad", "alice", SYMBOL, "BUY", "LIMIT", 10**9, 1)
    assert r.duplicate and r.reject_reason is RejectReason.INSUFFICIENT_FUNDS
    log.info("old-wal compat: O1=%s O2=%s reject-replay=ok",
             o1.status.value, o2.status.value)


# ---------- 并发 ----------

def test_concurrent_fok_atomicity(engine):
    """并发 FOK：要么全成要么整单失败，不存在半成交；账务守恒。"""
    engine.deposit_cash("alice", 10_000_000)
    engine.deposit_position("carol", SYMBOL, 100)
    engine.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 100, 50)

    def submit(i):
        return engine.submit_order(f"f{i}", "alice", SYMBOL, "BUY", "LIMIT",
                                   100, 10, time_in_force="FOK")

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(submit, range(20)))

    filled = [r for r in results if r.status is OrderStatus.FILLED]
    rejected = [r for r in results if r.status is OrderStatus.REJECTED]
    assert len(filled) == 5 and len(rejected) == 15
    assert all(r.reject_reason is RejectReason.FOK_UNFILLABLE for r in rejected)
    # 成交总额 = 5*10*100，失败单无冻结残留
    snap = engine.snapshot_account("alice")
    assert snap["cash_available"] == 10_000_000 - 5 * 10 * 100
    assert snap["cash_frozen"] == 0
    assert engine.total_cash() == 10_000_000
    assert engine.total_position(SYMBOL) == 100
    log.info("concurrent fok: filled=%d rejected=%d frozen=%d conservation=ok",
             len(filled), len(rejected), snap["cash_frozen"])
