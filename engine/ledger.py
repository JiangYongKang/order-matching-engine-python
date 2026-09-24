"""账户账务：资金与持仓的冻结、扣减、释放。

不变量（任意时刻成立，可用于守恒校验）：
- cash_available >= 0, cash_frozen >= 0
- position.available >= 0, position.frozen >= 0
- 全系统 total_cash 与 total_position 守恒（充值除外）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Position:
    available: int = 0
    frozen: int = 0


@dataclass
class Account:
    account_id: str
    cash_available: int = 0
    cash_frozen: int = 0
    positions: dict[str, Position] = field(default_factory=dict)

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position())


class Ledger:
    """多账户账务表。所有金额/数量为非负整数（分/股）。

    本类不做并发控制，由上层引擎在锁内调用。
    """

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}

    def get_or_create(self, account_id: str) -> Account:
        return self._accounts.setdefault(account_id, Account(account_id))

    def account(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    # ---- 充值（测试/初始化用） ----
    def deposit_cash(self, account_id: str, amount: int) -> None:
        assert amount > 0
        self.get_or_create(account_id).cash_available += amount

    def deposit_position(self, account_id: str, symbol: str, qty: int) -> None:
        assert qty > 0
        acc = self.get_or_create(account_id)
        acc.position(symbol).available += qty

    # ---- 冻结 / 释放 ----
    def freeze_cash(self, account_id: str, amount: int) -> bool:
        acc = self.get_or_create(account_id)
        if acc.cash_available < amount:
            return False
        acc.cash_available -= amount
        acc.cash_frozen += amount
        return True

    def unfreeze_cash(self, account_id: str, amount: int) -> None:
        acc = self.get_or_create(account_id)
        assert acc.cash_frozen >= amount, "释放超过冻结额"
        acc.cash_frozen -= amount
        acc.cash_available += amount

    def freeze_position(self, account_id: str, symbol: str, qty: int) -> bool:
        pos = self.get_or_create(account_id).position(symbol)
        if pos.available < qty:
            return False
        pos.available -= qty
        pos.frozen += qty
        return True

    def unfreeze_position(self, account_id: str, symbol: str, qty: int) -> None:
        pos = self.get_or_create(account_id).position(symbol)
        assert pos.frozen >= qty, "释放超过冻结量"
        pos.frozen -= qty
        pos.available += qty

    # ---- 成交结算 ----
    def settle_trade(self, buy_account: str, sell_account: str,
                     symbol: str, price: int, qty: int,
                     buy_frozen_price: int) -> None:
        """按成交结果结算。

        买方：按下单时冻结单价 buy_frozen_price 冻结了 qty*buy_frozen_price，
              实际成交价为 price（<= 冻结价），差额退回可用。
        卖方：冻结持仓扣减 qty，收到 qty*price 现金。
        """
        buyer = self.get_or_create(buy_account)
        seller = self.get_or_create(sell_account)

        frozen_amount = qty * buy_frozen_price
        cost = qty * price
        assert buyer.cash_frozen >= frozen_amount, "买方冻结资金不足"
        buyer.cash_frozen -= frozen_amount
        buyer.cash_available += frozen_amount - cost  # 多退
        buyer.position(symbol).available += qty

        pos = seller.position(symbol)
        assert pos.frozen >= qty, "卖方冻结持仓不足"
        pos.frozen -= qty
        seller.cash_available += cost

    # ---- 守恒校验 ----
    def total_cash(self) -> int:
        return sum(a.cash_available + a.cash_frozen for a in self._accounts.values())

    def total_position(self, symbol: str) -> int:
        return sum(
            p.available + p.frozen
            for a in self._accounts.values()
            for s, p in a.positions.items() if s == symbol
        )
