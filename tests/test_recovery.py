"""进程重启后状态重建与重启前一致。"""

import logging
from decimal import Decimal as D

from app.engine import MatchingEngine
from app.enums import OrderStatus, OrderType, Side
from app.log_store import EventLog
from conftest import place

log = logging.getLogger("app.test.recovery")


def _snapshot(eng):
    return {
        "accounts": {a: eng.account_snapshot(a)
                     for a in ("BUYER", "SELLER", "RICH")},
        "book": eng.book_snapshot("AAA"),
        "fills": eng.fills(),
        "orders": {
            oid: (o.status.value, str(o.filled_qty), str(o.avg_fill_price))
            for oid, o in eng.orders_by_id.items()
        },
    }


def test_state_rebuilds_identically_after_restart(tmp_path):
    path = str(tmp_path / "events.jsonl")

    log1 = EventLog(path).open()
    eng = MatchingEngine(log1, ["AAA", "BBB"])
    eng.create_account("BUYER", cash=D("10000"))
    eng.create_account("SELLER", cash=D("1000"), positions={"AAA": D("100")})
    eng.create_account("RICH", cash=D("1000000"),
                       positions={"AAA": D("100000")})

    s1 = place(eng, "s1", "SELLER", Side.SELL, "10", "100")
    place(eng, "b1", "BUYER", Side.BUY, "4", "100")     # 部分成交 s1
    eng.cancel_order(s1.order_id)                        # 撤剩余 6
    resting = place(eng, "b2", "BUYER", Side.BUY, "3", "90")  # 挂买簿
    place(eng, "mb", "BUYER", Side.BUY, "9", None,
          otype=OrderType.MARKET, max_price="95")        # 无卖单->全作废
    # 一笔拒单（应可重放且不影响余额）
    try:
        place(eng, "rej", "BUYER", Side.BUY, "99999999", "100")
    except Exception:
        pass
    before = _snapshot(eng)
    log1.close()

    # 模拟进程重启：新建引擎仅重放
    log2 = EventLog(path).open()
    eng2 = MatchingEngine(log2, ["AAA", "BBB"])
    eng2.recover()
    after = _snapshot(eng2)

    log.info("WAL=%s 判定=重放事件重建状态 订单数=%s 成交数=%s 挂买=%s",
             path, len(after["orders"]), len(after["fills"]),
             after["book"]["bids"])
    assert before == after

    # 幂等映射恢复：重复 b2 返回同一订单，不新增成交
    dup = place(eng2, "b2", "BUYER", Side.BUY, "3", "90")
    assert dup.order_id == resting.order_id
    assert len(eng2.fills()) == len(before["fills"])

    # 恢复后仍可继续撮合：新卖单与挂在 90 的 b2 成交
    s2 = place(eng2, "s2", "SELLER", Side.SELL, "3", "90")
    assert s2.status is OrderStatus.FILLED
    assert eng2.get_order(resting.order_id).status is OrderStatus.FILLED


def test_recover_is_idempotent(engine, tmp_path):
    """对同一日志多次 recover 结果稳定（可安全重入）。"""
    place(engine, "s", "SELLER", Side.SELL, "5", "100")
    place(engine, "b", "BUYER", Side.BUY, "5", "100")
    # 把内存事件复制到文件日志用于重放
    path = tmp_path / "e.jsonl"
    import json
    with open(path, "w", encoding="utf-8") as fh:
        for ev in engine.log.read_all():
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    e2 = MatchingEngine(EventLog(str(path)).open(), ["AAA", "BBB"])
    e2.recover()
    snap1 = _snapshot(e2)
    e2.recover()
    snap2 = _snapshot(e2)
    assert snap1 == snap2
    log.info("判定=重复recover结果稳定 成交=%s 订单=%s",
             len(snap1["fills"]), len(snap1["orders"]))


def test_recovery_replays_matches_lost_in_crash_window(tmp_path):
    """模拟崩溃窗口：买卖单的 ORDER_ACCEPTED 都已 fsync，
    但 ORDER_FILLED 尚未落盘。重启后应确定性补回成交，账务守恒。"""
    path = tmp_path / "crash.jsonl"

    # 第一个进程：开户 + 卖单 + 买单（正常会立即成交）
    elog = EventLog(str(path)).open()
    eng = MatchingEngine(elog, ["AAA"])
    eng.create_account("BUYER", cash=D("10000"))
    eng.create_account("SELLER", cash=D("1000"), positions={"AAA": D("100")})
    s = place(eng, "s1", "SELLER", Side.SELL, "10", "100")
    b = place(eng, "b1", "BUYER", Side.BUY, "10", "100")
    assert b.status is OrderStatus.FILLED
    elog.close()

    # 模拟崩溃：只保留到最后一条 ORDER_ACCEPTED（剔除 ORDER_FILLED）。
    import json
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    kept, accepted_seen = [], 0
    for line in lines:
        ev = json.loads(line)
        if ev["type"] == "ORDER_ACCEPTED":
            accepted_seen += 1
            kept.append(line)
        elif ev["type"] == "ACCOUNT_OPENED":
            kept.append(line)
        # 丢弃 ORDER_FILLED
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    assert accepted_seen == 2

    # 重启：两单零成交重放，但互为对手 -> 确定性补撮合
    log2 = EventLog(str(path)).open()
    eng2 = MatchingEngine(log2, ["AAA"])
    eng2.recover()

    bs = eng2.account_snapshot("BUYER")
    ss = eng2.account_snapshot("SELLER")
    log.info("崩溃恢复 买订单=%s 状态=%s 卖订单=%s 状态=%s 买方现金=%s 卖方现金=%s",
             b.order_id, eng2.get_order(b.order_id).status.value,
             s.order_id, eng2.get_order(s.order_id).status.value,
             bs["cash_total"], ss["cash_total"])
    assert eng2.get_order(b.order_id).status is OrderStatus.FILLED
    assert eng2.get_order(s.order_id).status is OrderStatus.FILLED
    # 买方花 1000，得 10 券；卖方得 1000，余 90 券
    assert bs["cash_total"] == D("9000")
    assert bs["position_total"]["AAA"] == D("10")
    assert ss["cash_total"] == D("2000")
    assert ss["position_total"]["AAA"] == D("90")
    # 补回的成交带 recovered 标记
    recovered_fills = [f for f in eng2.fills() if f.get("recovered")]
    assert len(recovered_fills) == 1
    # 再次重启不会重复补成交（幂等）
    log2.close()
    log3 = EventLog(str(path)).open()
    eng3 = MatchingEngine(log3, ["AAA"])
    eng3.recover()
    assert len([f for f in eng3.fills()]) == 1
    assert eng3.account_snapshot("BUYER")["cash_total"] == D("9000")
