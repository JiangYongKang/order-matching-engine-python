"""HTTP 接口层测试：下单/撤单/查询/账户，含拒单 422 与幂等标记。"""
import pytest
from fastapi.testclient import TestClient

import main
from engine.engine import MatchingEngine


@pytest.fixture
def client(tmp_path, monkeypatch):
    main.engine = MatchingEngine(main.SYMBOLS, tmp_path / "wal.jsonl")
    return TestClient(main.app)


def _order(key, account, side, otype="LIMIT", price=500, qty=1, symbol="BTC-USDT"):
    return {"idempotency_key": key, "account_id": account, "symbol": symbol,
            "side": side, "order_type": otype, "price": price, "quantity": qty}


def test_full_flow(client):
    client.post("/accounts/s1/deposit", json={"symbol": "BTC-USDT", "quantity": 10})
    client.post("/accounts/b1/deposit", json={"amount": 100000})

    r = client.post("/orders", json=_order("k1", "s1", "SELL", qty=5))
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED"

    r = client.post("/orders", json=_order("k2", "b1", "BUY", qty=5))
    body = r.json()
    assert body["status"] == "FILLED" and len(body["trades"]) == 1

    r = client.get(f"/orders/{body['order_id']}")
    assert r.json()["filled"] == 5 and r.json()["status"] == "FILLED"

    r = client.get("/trades", params={"symbol": "BTC-USDT"})
    assert len(r.json()) == 1

    r = client.get("/accounts/b1")
    assert r.json()["positions"]["BTC-USDT"]["available"] == 5


def test_reject_returns_422_with_reason(client):
    client.post("/accounts/b1/deposit", json={"amount": 10})
    r = client.post("/orders", json=_order("k1", "b1", "BUY", price=99999, qty=5))
    assert r.status_code == 422
    assert r.json()["detail"]["reject_reason"] == "INSUFFICIENT_FUNDS"


def test_duplicate_returns_same_order(client):
    client.post("/accounts/b1/deposit", json={"amount": 100000})
    r1 = client.post("/orders", json=_order("k1", "b1", "BUY"))
    r2 = client.post("/orders", json=_order("k1", "b1", "BUY"))
    assert r1.json()["order_id"] == r2.json()["order_id"]
    assert r2.json()["duplicate"] is True


def test_cancel_endpoints(client):
    client.post("/accounts/b1/deposit", json={"amount": 100000})
    oid = client.post("/orders", json=_order("k1", "b1", "BUY")).json()["order_id"]
    r = client.post(f"/orders/{oid}/cancel")
    assert r.json()["status"] == "CANCELLED"
    r = client.post(f"/orders/{oid}/cancel")  # 幂等
    assert r.json()["status"] == "CANCELLED"
    assert client.post("/orders/O999/cancel").status_code == 404
    assert client.get("/orders/O999").status_code == 404


def test_new_params_visible_via_api(client):
    """新参数与最终结论（含 FOK 整单失败）可通过查询接口看到。"""
    client.post("/accounts/s1/deposit", json={"symbol": "BTC-USDT", "quantity": 2})
    client.post("/accounts/b1/deposit", json={"amount": 100000})
    client.post("/orders", json=_order("k1", "s1", "SELL", price=500, qty=2))

    # FOK 流动性不足 -> 422 + FOK_UNFILLABLE，且订单可查
    body = _order("k2", "b1", "BUY", price=600, qty=10)
    body["time_in_force"] = "FOK"
    r = client.post("/orders", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["reject_reason"] == "FOK_UNFILLABLE"
    o = client.get(f"/orders/{detail['order_id']}").json()
    assert o["status"] == "REJECTED" and o["time_in_force"] == "FOK"
    assert o["reject_reason"] == "FOK_UNFILLABLE"

    # 市价买带保护上限：超出档不吃，结论与生效保护价可查
    body = _order("k3", "b1", "BUY", otype="MARKET", price=0, qty=5)
    body["protection_price"] = 550
    r = client.post("/orders", json=body)
    assert r.status_code == 200
    o = client.get(f"/orders/{r.json()['order_id']}").json()
    assert o["protection_price"] == 550 and o["time_in_force"] == "IOC"
    assert o["filled"] == 2 and o["status"] == "CANCELLED"

    # 不传新参数：默认口径（限价 GTC），老调用无需改动
    r = client.post("/orders", json=_order("k4", "b1", "BUY", price=100, qty=1))
    o = client.get(f"/orders/{r.json()['order_id']}").json()
    assert o["time_in_force"] == "GTC" and o["protection_price"] == 0
