"""成交方式（GTC/IOC/FOK）与市价单价格保护。

覆盖：FOK 整单成交/整单失败无副作用、IOC 剩余撤销、市价买/卖价格保护与
安全默认、非法参数组合、幂等回放、重启恢复一致、旧版 WAL 兼容、并发守恒。
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from engine.engine import MatchingEngine
from engine.models import OrderStatus, RejectReason, TimeInForce
from tests.conftest import SYMBOL

log = logging.getLogger("matching")


def _log_decision(tag, r):
    log.info("%s id=%s status=%s reason=%s trades=%d", tag, r.order_id,
             r.status.value, r.reject_reason.value, len(r.trades))


# ---------- FOK：立即全部成交，否则整单不成交 ----------

def test_fok_limit_filled(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 5)
    r = funded.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 500, 5,
                            time_in_force="FOK")
    _log_decision("fok-limit-fill", r)
    assert r.status is OrderStatus.FILLED and len(r.trades) == 1
    o = funded.get_order(r.order_id)
    assert o.time_in_force is TimeInForce.FOK
    assert funded.snapshot_account("alice")["cash_frozen"] == 0


def test_fok_limit_insufficient_rejected_no_side_effects(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    cash0 = funded.snapshot_account("alice")
    pos0 = funded.snapshot_account("carol")

    r = funded.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 500, 5,
                            time_in_force="FOK")
    _log_decision("fok-limit-reject", r)
    assert not r.accepted
    assert r.reject_reason is RejectReason.FOK_NOT_FULLY_FILLABLE
    # 整单失败：无成交、无冻结残留、双方账务逐项不变、卖单原样挂盘
    assert funded.list_trades() == []
    assert funded.snapshot_account("alice") == cash0
    assert funded.snapshot_account("carol") == pos0
    assert funded.get_order("O1").remaining == 3
    assert funded.total_cash() == 2_000_000
    assert funded.total_position(SYMBOL) == 2_000


def test_fok_limit_price_too_low_rejected(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 10)
    r = funded.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 400, 5,
                            time_in_force="FOK")
    _log_decision("fok-price-miss", r)
    assert r.reject_reason is RejectReason.FOK_NOT_FULLY_FILLABLE
    assert funded.snapshot_account("alice")["cash_available"] == 1_000_000


def test_fok_market_buy_cap_blocks_fill(funded):
    """市价 FOK 买：保护价 550 时仅 3 股可成交 < 委托 5 股，整单失败。"""
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    funded.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 600, 3)
    cash0 = funded.snapshot_account("alice")

    r = funded.submit_order("f1", "alice", SYMBOL, "BUY", "MARKET", 0, 5,
                            time_in_force="FOK", protection_price=550)
    _log_decision("fok-market-cap-reject", r)
    assert r.reject_reason is RejectReason.FOK_NOT_FULLY_FILLABLE
    assert funded.list_trades() == []
    assert funded.snapshot_account("alice") == cash0

    # 放宽保护价到 600：可成交 6 >= 5，整单成交
    r2 = funded.submit_order("f2", "alice", SYMBOL, "BUY", "MARKET", 0, 5,
                             time_in_force="FOK", protection_price=600)
    _log_decision("fok-market-fill", r2)
    assert r2.status is OrderStatus.FILLED
    assert [(t.price, t.quantity) for t in r2.trades] == [(500, 3), (600, 2)]
    # 冻结 600*5=3000，实付 1500+1200=2700，差额退回
    snap = funded.snapshot_account("alice")
    assert snap["cash_available"] == 1_000_000 - 2700
    assert snap["cash_frozen"] == 0
    assert funded.total_cash() == 2_000_000


def test_fok_market_sell_insufficient_rejected(funded):
    funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 2)
    pos0 = funded.snapshot_account("carol")
    r = funded.submit_order("f1", "carol", SYMBOL, "SELL", "MARKET", 0, 5,
                            time_in_force="FOK")
    _log_decision("fok-sell-reject", r)
    assert r.reject_reason is RejectReason.FOK_NOT_FULLY_FILLABLE
    assert funded.list_trades() == []
    assert funded.snapshot_account("carol") == pos0


# ---------- IOC：立即成交，剩余自动撤销 ----------

def test_ioc_limit_buy_partial_remainder_cancelled(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    r = funded.submit_order("i1", "alice", SYMBOL, "BUY", "LIMIT", 550, 5,
                            time_in_force="IOC")
    _log_decision("ioc-buy-partial", r)
    o = funded.get_order(r.order_id)
    assert sum(t.quantity for t in r.trades) == 3
    assert o.status is OrderStatus.CANCELLED and o.remaining == 2
    snap = funded.snapshot_account("alice")
    assert snap["cash_frozen"] == 0
    assert snap["cash_available"] == 1_000_000 - 3 * 500
    assert funded.total_cash() == 2_000_000


def test_ioc_limit_sell_partial_remainder_cancelled(funded):
    funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 3)
    r = funded.submit_order("i1", "dave", SYMBOL, "SELL", "LIMIT", 450, 5,
                            time_in_force="IOC")
    _log_decision("ioc-sell-partial", r)
    o = funded.get_order(r.order_id)
    assert sum(t.quantity for t in r.trades) == 3
    assert o.status is OrderStatus.CANCELLED and o.remaining == 2
    pos = funded.snapshot_account("dave")["positions"][SYMBOL]
    assert pos["frozen"] == 0 and pos["available"] == 1_000 - 3
    assert funded.total_position(SYMBOL) == 2_000


def test_ioc_no_match_cancelled_immediately(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    r = funded.submit_order("i1", "alice", SYMBOL, "BUY", "LIMIT", 400, 5,
                            time_in_force="IOC")
    _log_decision("ioc-no-match", r)
    assert r.trades == []
    assert funded.get_order(r.order_id).status is OrderStatus.CANCELLED
    assert funded.snapshot_account("alice")["cash_frozen"] == 0


# ---------- 市价单价格保护 ----------

def test_market_buy_protection_cap(funded):
    """市价买保护价 550：只吃 500 档，600 档触保护不成交，剩余撤销。"""
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    funded.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 600, 3)
    r = funded.submit_order("m1", "alice", SYMBOL, "BUY", "MARKET", 0, 5,
                            protection_price=550)
    _log_decision("market-buy-cap", r)
    o = funded.get_order(r.order_id)
    assert [(t.price, t.quantity) for t in r.trades] == [(500, 3)]
    assert o.status is OrderStatus.CANCELLED and o.remaining == 2
    assert funded.get_order("O2").remaining == 3  # 600 档未被触碰
    snap = funded.snapshot_account("alice")
    assert snap["cash_frozen"] == 0
    assert snap["cash_available"] == 1_000_000 - 1500
    assert funded.total_cash() == 2_000_000


def test_market_buy_default_cap_is_best_ask(funded):
    """不带保护价的安全默认：有效上限=下单时最优卖价，不向上吃更贵的档。"""
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    funded.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 600, 3)
    r = funded.submit_order("m1", "alice", SYMBOL, "BUY", "MARKET", 0, 5)
    _log_decision("market-buy-default-cap", r)
    o = funded.get_order(r.order_id)
    assert [(t.price, t.quantity) for t in r.trades] == [(500, 3)]
    assert o.status is OrderStatus.CANCELLED and o.remaining == 2
    # 成交总额 1500 不超过下单时锁定额度 500*5=2500，冻结全额释放
    snap = funded.snapshot_account("alice")
    assert snap["cash_frozen"] == 0
    assert snap["cash_available"] == 1_000_000 - 1500


def test_market_sell_protection_floor(funded):
    """市价卖保护价 450：只吃 500 档，400 档触保护不成交，剩余撤销。"""
    funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 3)
    funded.submit_order("b2", "bob", SYMBOL, "BUY", "LIMIT", 400, 3)
    r = funded.submit_order("m1", "carol", SYMBOL, "SELL", "MARKET", 0, 5,
                            protection_price=450)
    _log_decision("market-sell-floor", r)
    o = funded.get_order(r.order_id)
    assert [(t.price, t.quantity) for t in r.trades] == [(500, 3)]
    assert o.status is OrderStatus.CANCELLED and o.remaining == 2
    assert funded.snapshot_account("carol")["cash_available"] == 1500
    pos = funded.snapshot_account("carol")["positions"][SYMBOL]
    assert pos["frozen"] == 0 and pos["available"] == 1_000 - 3


def test_market_sell_default_no_floor(funded):
    """市价卖不带保护价：默认下限 0（不约束），可向下吃完。"""
    funded.submit_order("b1", "alice", SYMBOL, "BUY", "LIMIT", 500, 3)
    funded.submit_order("b2", "bob", SYMBOL, "BUY", "LIMIT", 400, 3)
    r = funded.submit_order("m1", "carol", SYMBOL, "SELL", "MARKET", 0, 5)
    _log_decision("market-sell-default", r)
    assert r.status is OrderStatus.FILLED
    assert [(t.price, t.quantity) for t in r.trades] == [(500, 3), (400, 2)]
    assert funded.snapshot_account("carol")["cash_available"] == 1500 + 800


# ---------- 非法参数组合 ----------

def test_invalid_param_combinations(funded):
    r = funded.submit_order("g1", "alice", SYMBOL, "BUY", "MARKET", 0, 1,
                            time_in_force="GTC")
    assert r.reject_reason is RejectReason.INVALID_ORDER  # 市价单不能挂单

    r = funded.submit_order("g2", "alice", SYMBOL, "BUY", "LIMIT", 100, 1,
                            protection_price=500)
    assert r.reject_reason is RejectReason.INVALID_ORDER  # 保护价仅市价单

    r = funded.submit_order("g3", "alice", SYMBOL, "BUY", "LIMIT", 100, 1,
                            time_in_force="DAY")
    assert r.reject_reason is RejectReason.INVALID_ORDER  # 未知成交方式
    log.info("invalid combos rejected: market+GTC / limit+protection / bad tif")


# ---------- 幂等 ----------

def test_fok_reject_idempotent_replay(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    r1 = funded.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 500, 5,
                             time_in_force="FOK")
    assert r1.reject_reason is RejectReason.FOK_NOT_FULLY_FILLABLE
    cash0 = funded.snapshot_account("alice")
    r2 = funded.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 500, 5,
                             time_in_force="FOK")
    assert r2.duplicate and r2.reject_reason is RejectReason.FOK_NOT_FULLY_FILLABLE
    assert funded.snapshot_account("alice") == cash0
    log.info("fok reject idempotent: key=f1 reason=%s", r2.reject_reason.value)


def test_ioc_duplicate_no_reexecution(funded):
    funded.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 3)
    r1 = funded.submit_order("i1", "alice", SYMBOL, "BUY", "LIMIT", 550, 5,
                             time_in_force="IOC")
    r2 = funded.submit_order("i1", "alice", SYMBOL, "BUY", "LIMIT", 550, 5,
                             time_in_force="IOC")
    assert r2.duplicate and r2.order_id == r1.order_id
    assert len(funded.list_trades()) == 1
    assert funded.snapshot_account("alice")["cash_available"] == 1_000_000 - 1500


# ---------- 重启恢复 ----------

def _full_state(e):
    return {
        "cash": e.total_cash(),
        "pos": e.total_position(SYMBOL),
        "alice": e.snapshot_account("alice"),
        "carol": e.snapshot_account("carol"),
        "dave": e.snapshot_account("dave"),
        "orders": {oid: (o.status, o.remaining, o.time_in_force, o.protection_price)
                   for i in range(1, 30)
                   if (o := e.get_order(f"O{i}")) is not None
                   for oid in [o.order_id]},
        "trades": [(t.trade_id, t.price, t.quantity) for t in e.list_trades()],
    }


def test_restart_recovery_with_tif_protection(tmp_path):
    wal = tmp_path / "wal.jsonl"
    e1 = MatchingEngine([SYMBOL], wal)
    e1.deposit_cash("alice", 1_000_000)
    e1.deposit_cash("bob", 1_000_000)
    e1.deposit_position("carol", SYMBOL, 1_000)
    e1.deposit_position("dave", SYMBOL, 1_000)
    e1.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 500, 10)
    e1.submit_order("s2", "dave", SYMBOL, "SELL", "LIMIT", 600, 5)
    e1.submit_order("m1", "alice", SYMBOL, "BUY", "MARKET", 0, 4,
                    protection_price=550)                       # 市价+保护 FILLED
    e1.submit_order("i1", "alice", SYMBOL, "BUY", "LIMIT", 550, 8,
                    time_in_force="IOC")                        # 部分成交后撤销
    r_fok_rej = e1.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 700, 10,
                                time_in_force="FOK")            # 整单失败
    assert r_fok_rej.reject_reason is RejectReason.FOK_NOT_FULLY_FILLABLE
    e1.submit_order("f2", "alice", SYMBOL, "BUY", "MARKET", 0, 3,
                    time_in_force="FOK", protection_price=600)  # FOK 成交
    e1.submit_order("g1", "bob", SYMBOL, "BUY", "LIMIT", 300, 2,
                    time_in_force="GTC")                        # 显式 GTC 挂单
    before = _full_state(e1)

    e2 = MatchingEngine([SYMBOL], wal)  # 模拟重启
    after = _full_state(e2)
    assert before == after
    log.info("recovery tif/protection: orders=%s consistent=True",
             {k: v[0].value for k, v in after["orders"].items()})

    # 恢复后幂等仍生效（含 FOK 拒单与市价保护单）
    assert e2.submit_order("f1", "alice", SYMBOL, "BUY", "LIMIT", 700, 10,
                           time_in_force="FOK").duplicate
    rdup = e2.submit_order("m1", "alice", SYMBOL, "BUY", "MARKET", 0, 4,
                           protection_price=550)
    assert rdup.duplicate and len(e2.list_trades()) == len(before["trades"])
    # 恢复后可继续撮合
    r = e2.submit_order("s3", "carol", SYMBOL, "SELL", "LIMIT", 300, 2)
    assert r.status is OrderStatus.FILLED  # 吃掉 g1@300
    assert e2.total_cash() == 2_000_000
    assert e2.total_position(SYMBOL) == 2_000


def test_legacy_wal_without_new_fields(tmp_path):
    """旧版 WAL（无 time_in_force/protection_price 字段）按原语义读入。"""
    wal = tmp_path / "wal.jsonl"
    legacy = [
        {"type": "DEPOSIT_CASH", "account_id": "alice", "amount": 1_000_000},
        {"type": "DEPOSIT_POSITION", "account_id": "carol", "symbol": SYMBOL,
         "qty": 1_000},
        {"type": "ORDER_ACCEPTED", "order_id": "O1", "account_id": "carol",
         "symbol": SYMBOL, "side": "SELL", "order_type": "LIMIT", "price": 500,
         "quantity": 10, "frozen_price": 0, "idempotency_key": "s1", "seq": 1},
        {"type": "ORDER_ACCEPTED", "order_id": "O2", "account_id": "alice",
         "symbol": SYMBOL, "side": "BUY", "order_type": "LIMIT", "price": 600,
         "quantity": 4, "frozen_price": 600, "idempotency_key": "b1", "seq": 2},
        {"type": "TRADE", "trade_id": "T3", "s": SYMBOL, "p": 500, "q": 4,
         "buy_order_id": "O2", "sell_order_id": "O1", "buy_account_id": "alice",
         "sell_account_id": "carol", "seq": 3},
        {"type": "ORDER_ACCEPTED", "order_id": "O4", "account_id": "alice",
         "symbol": SYMBOL, "side": "BUY", "order_type": "MARKET", "price": 0,
         "quantity": 2, "frozen_price": 500, "idempotency_key": "m1", "seq": 4},
        {"type": "TRADE", "trade_id": "T5", "s": SYMBOL, "p": 500, "q": 2,
         "buy_order_id": "O4", "sell_order_id": "O1", "buy_account_id": "alice",
         "sell_account_id": "carol", "seq": 5},
        {"type": "ORDER_CANCELLED", "order_id": "O1", "reason": "USER_CANCEL",
         "remaining": 4},
    ]
    wal.write_text("".join(json.dumps(e) + "\n" for e in legacy),
                   encoding="utf-8")

    e = MatchingEngine([SYMBOL], wal)
    o1, o2, o4 = e.get_order("O1"), e.get_order("O2"), e.get_order("O4")
    # 旧记录按旧默认归一化：LIMIT→GTC，MARKET→IOC，结论不变
    assert o1.status is OrderStatus.CANCELLED and o1.remaining == 4
    assert o1.time_in_force is TimeInForce.GTC and o1.protection_price == 0
    assert o2.status is OrderStatus.FILLED and o2.time_in_force is TimeInForce.GTC
    assert o4.status is OrderStatus.FILLED and o4.time_in_force is TimeInForce.IOC
    assert e.snapshot_account("alice")["cash_available"] == 1_000_000 - 6 * 500
    assert e.total_cash() == 1_000_000
    assert e.total_position(SYMBOL) == 1_000
    # 旧幂等键仍生效，新序号不冲突
    assert e.submit_order("m1", "alice", SYMBOL, "BUY", "MARKET", 0, 2).duplicate
    r = e.submit_order("b2", "alice", SYMBOL, "BUY", "LIMIT", 100, 1)
    assert r.order_id == "O6"
    log.info("legacy wal replayed: orders=%s new_order=%s",
             [f"O{i}" for i in range(1, 5)], r.order_id)


# ---------- 并发 ----------

def test_concurrent_fok_atomic(engine):
    """并发 FOK 抢同一份流动性：整单成交或整单失败，无中间态。"""
    engine.deposit_cash("alice", 10_000_000)
    engine.deposit_cash("bob", 10_000_000)
    engine.deposit_position("carol", SYMBOL, 10_000)
    engine.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 100, 2_000)

    def submit(tag):
        return engine.submit_order(f"f-{tag}", tag, SYMBOL, "BUY", "LIMIT",
                                   100, 1_500, time_in_force="FOK")

    with ThreadPoolExecutor(max_workers=2) as ex:
        r_alice, r_bob = ex.map(submit, ["alice", "bob"], timeout=10)
    results = [r_alice, r_bob]
    filled = [r for r in results if r.status is OrderStatus.FILLED]
    rejected = [r for r in results
                if r.reject_reason is RejectReason.FOK_NOT_FULLY_FILLABLE]
    assert len(filled) == 1 and len(rejected) == 1  # 恰有一单整成、一单整败
    assert sum(t.quantity for t in engine.list_trades()) == 1_500
    assert engine.total_cash() == 20_000_000
    assert engine.total_position(SYMBOL) == 10_000
    log.info("concurrent fok: filled=%s rejected=%s conservation=ok",
             [r.order_id for r in filled], len(rejected))


def test_concurrent_tif_mix_conservation(engine):
    """市价保护/IOC/FOK 混合并发：冻结不突破、成交总额不超锁定、总量守恒。"""
    engine.deposit_cash("alice", 10_000_000)
    engine.deposit_cash("bob", 10_000_000)
    engine.deposit_position("carol", SYMBOL, 10_000)
    engine.submit_order("s1", "carol", SYMBOL, "SELL", "LIMIT", 100, 2_000)

    def submit(i):
        account = "alice" if i % 2 == 0 else "bob"
        kind = i % 3
        if kind == 0:
            return engine.submit_order(f"m{i}", account, SYMBOL, "BUY",
                                       "MARKET", 0, 10, protection_price=100)
        if kind == 1:
            return engine.submit_order(f"i{i}", account, SYMBOL, "BUY",
                                       "LIMIT", 100, 10, time_in_force="IOC")
        return engine.submit_order(f"f{i}", account, SYMBOL, "BUY",
                                   "LIMIT", 100, 10, time_in_force="FOK")

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(submit, range(60)))

    traded = sum(t.quantity for t in engine.list_trades())
    assert traded <= 2_000
    # 全部成交或撤销/拒绝，无残留冻结
    for account in ("alice", "bob"):
        assert engine.snapshot_account(account)["cash_frozen"] == 0
    # 买方支出 == 卖方收入 == 成交量*100，总量守恒
    assert engine.snapshot_account("carol")["cash_available"] == traded * 100
    assert engine.total_cash() == 20_000_000
    assert engine.total_position(SYMBOL) == 10_000
    statuses = {}
    for r in results:
        statuses[r.status.value] = statuses.get(r.status.value, 0) + 1
    log.info("concurrent tif mix: traded=%d statuses=%s conservation=ok",
             traded, statuses)
