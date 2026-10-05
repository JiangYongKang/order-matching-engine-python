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


def test_tif_and_protection_via_api(client):
    """新参数经 HTTP 下单，结论与判定依据可通过查询接口看到。"""
    client.post("/accounts/s1/deposit", json={"symbol": "BTC-USDT", "quantity": 10})
    client.post("/accounts/b1/deposit", json={"amount": 100000})
    client.post("/orders", json=_order("k1", "s1", "SELL", price=500, qty=3))

    # FOK 流动性不足：整单失败，422 + 拒绝原因
    r = client.post("/orders", json=_order("k2", "b1", "BUY", price=500, qty=5)
                    | {"time_in_force": "FOK"})
    assert r.status_code == 422
    assert r.json()["detail"]["reject_reason"] == "FOK_NOT_FULLY_FILLABLE"

    # IOC 部分成交：剩余撤销，查询接口可见成交方式与最终状态
    r = client.post("/orders", json=_order("k3", "b1", "BUY", price=600, qty=5)
                    | {"time_in_force": "IOC"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    o = client.get(f"/orders/{r.json()['order_id']}").json()
    assert o["time_in_force"] == "IOC" and o["filled"] == 3 and o["remaining"] == 2

    # 市价买 + 价格保护：查询接口可见保护价
    client.post("/orders", json=_order("k4", "s1", "SELL", price=700, qty=3))
    r = client.post("/orders", json=_order("k5", "b1", "BUY", otype="MARKET",
                                           price=0, qty=2)
                    | {"protection_price": 650})
    assert r.status_code == 200
    o = client.get(f"/orders/{r.json()['order_id']}").json()
    assert o["protection_price"] == 650 and o["time_in_force"] == "IOC"
    assert o["filled"] == 0  # 700 档触保护价未成交

    # 不传新参数的旧式调用：行为不变（LIMIT→GTC 挂单）
    r = client.post("/orders", json=_order("k6", "b1", "BUY", price=100, qty=1))
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED"
    o = client.get(f"/orders/{r.json()['order_id']}").json()
    assert o["time_in_force"] == "GTC" and o["protection_price"] == 0
