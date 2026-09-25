"""HTTP 接口集成测试：下单、撤单、查询、拒单、幂等。"""

import logging

from fastapi.testclient import TestClient

log = logging.getLogger("app.test.api")


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MATCHING_ENGINE_WAL", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("MATCHING_ENGINE_SYMBOLS", "AAA")
    # 确保以环境变量重新装配应用
    import importlib

    import app.api as api_mod
    importlib.reload(api_mod)
    return TestClient(api_mod.app), api_mod.app


def test_order_lifecycle_and_idempotency(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)

    r = c.post("/orders", json={
        "client_order_id": "w1", "account_id": "ACC-2", "symbol": "AAA",
        "side": "SELL", "order_type": "LIMIT",
        "qty": "5", "price": "100"})
    assert r.status_code == 200, r.text
    sell = r.json()
    assert sell["status"] == "NEW"

    r = c.post("/orders", json={
        "client_order_id": "n1", "account_id": "ACC-1", "symbol": "AAA",
        "side": "BUY", "order_type": "LIMIT",
        "qty": "3", "price": "100"})
    buy = r.json()
    log.info("订单=%s 判定=HTTP下单立即成交 最终状态=%s filled=%s",
             buy["order_id"], buy["status"], buy["filled_qty"])
    assert buy["status"] == "FILLED" and buy["filled_qty"] == "3"

    # 幂等：重复提交返回同一订单
    r2 = c.post("/orders", json={
        "client_order_id": "n1", "account_id": "ACC-1", "symbol": "AAA",
        "side": "BUY", "order_type": "LIMIT",
        "qty": "3", "price": "100"})
    assert r2.json()["order_id"] == buy["order_id"]
    assert len(c.get("/fills").json()["fills"]) == 1

    # 撤剩余卖方 2
    rc = c.post(f"/orders/{sell['order_id']}/cancel").json()
    assert rc["canceled"] is True and rc["canceled_qty"] == "2"
    assert c.get(f"/orders/{sell['order_id']}").json()["status"] == "CANCELED"
    # 再撤终态订单 -> 不撤销，原因可区分
    rc2 = c.post(f"/orders/{sell['order_id']}/cancel").json()
    assert rc2["canceled"] is False and rc2["reason"] == "ALREADY_TERMINAL"
    log.info("订单=%s 判定=终态重复撤单 reason=%s",
             sell["order_id"], rc2["reason"])


def test_risk_rejection_reasons_over_http(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)

    def reject(payload, reason):
        r = c.post("/orders", json=payload)
        assert r.status_code == 400, r.text
        body = r.json()["detail"]
        log.info("拒单 client=%s 判定=%s detail=%s",
                 payload.get("client_order_id"), body["reason"],
                 body["detail"])
        assert body["reason"] == reason

    base = {"account_id": "ACC-1", "symbol": "AAA", "side": "BUY",
            "order_type": "LIMIT", "qty": "1"}
    reject({"client_order_id": "a", **base, "account_id": "NOPE"},
           "UNKNOWN_ACCOUNT")
    reject({"client_order_id": "b", **base, "symbol": "ZZZ"},
           "UNKNOWN_SYMBOL")
    reject({"client_order_id": "c", **base, "price": "0"}, "INVALID_PRICE")
    reject({"client_order_id": "d", **base, "qty": "0"}, "INVALID_QTY")
    reject({"client_order_id": "e", "account_id": "ACC-1", "symbol": "AAA",
            "side": "BUY", "order_type": "MARKET", "qty": "1"},
           "MARKET_BUY_NEEDS_PROTECTION")
    reject({"client_order_id": "f", **base, "price": "999999999"},
           "INSUFFICIENT_AVAILABLE_CASH")


def test_get_missing_entities_404(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    assert c.get("/orders/NOPE").status_code == 404
    assert c.get("/accounts/GHOST").status_code == 404


def test_book_and_account_snapshots(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    c.post("/orders", json={
        "client_order_id": "w", "account_id": "ACC-2", "symbol": "AAA",
        "side": "SELL", "order_type": "LIMIT", "qty": "7", "price": "101"})
    book = c.get("/books/AAA").json()
    assert float(book["asks"][0]["price"]) == 101
    acct = c.get("/accounts/ACC-2").json()
    # 卖出 7 张已冻结持仓（FastAPI 将 Decimal 序列化为数值）
    assert float(acct["position_frozen"]["AAA"]) == 7
