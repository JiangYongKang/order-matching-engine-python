"""账户资金与持仓：冻结、扣减、释放。

账户不变式（任意时刻，撮合全程总量守恒）：
  总现金   = cash_available + cash_frozen
  品种持仓 = position_available[s] + position_frozen[s]
  available / frozen 均不得为负。

下单即冻结：买单冻结资金 = qty * freeze_price（限价为 limit，
市价买为保护价 max_price）；卖单冻结持仓 = qty。成交时从冻结中
扣减对应部分；撤单则把剩余冻结原路释放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.enums import OrderRejected, RejectReason, Side
from app.models import Fill

ZERO = Decimal(0)


@dataclass
class Account:
    account_id: str
    cash_available: Decimal = ZERO
    cash_frozen: Decimal = ZERO
    position_available: dict[str, Decimal] = field(default_factory=dict)
    position_frozen: dict[str, Decimal] = field(default_factory=dict)

    @property
    def cash_total(self) -> Decimal:
        return self.cash_available + self.cash_frozen

    def position_total(self, symbol: str) -> Decimal:
        return (
            self.position_available.get(symbol, ZERO)
            + self.position_frozen.get(symbol, ZERO)
        )

    def snapshot(self) -> dict:
        return {
            "account_id": self.account_id,
            "cash_available": self.cash_available,
            "cash_frozen": self.cash_frozen,
            "cash_total": self.cash_total,
            "position_available": dict(self.position_available),
            "position_frozen": dict(self.position_frozen),
            "position_total": {
                s: self.position_total(s)
                for s in (
                    set(self.position_available) | set(self.position_frozen)
                )
            },
        }


class AccountBook:
    """账户集合。方法不自带锁，由引擎统一持锁调用（保证判定与记账原子）。"""

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}

    # ---- 查询 / 开户 ----------------------------------------------------
    def create_account(
        self,
        account_id: str,
        cash: Decimal = ZERO,
        positions: dict[str, Decimal] | None = None,
    ) -> Account:
        if account_id in self._accounts:
            raise ValueError(f"账户已存在: {account_id}")
        acct = Account(
            account_id=account_id,
            cash_available=Decimal(cash),
            position_available={
                s: Decimal(q) for s, q in (positions or {}).items()
            },
        )
        self._accounts[account_id] = acct
        return acct

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def require(self, account_id: str) -> Account:
        """账户必须存在，否则按风控拒单（原因可区分）。"""
        acct = self._accounts.get(account_id)
        if acct is None:
            raise OrderRejected(RejectReason.UNKNOWN_ACCOUNT)
        return acct

    # ---- 冻结 / 释放 ----------------------------------------------------
    def freeze_for_order(
        self,
        account: Account,
        symbol: str,
        side: Side,
        qty: Decimal,
        freeze_price: Decimal,
    ) -> Decimal:
        """下单冻结。买单返回冻结资金额；卖单冻结持仓，返回 ZERO。"""
        if side is Side.BUY:
            need = qty * freeze_price
            if account.cash_available < need:
                raise OrderRejected(
                    RejectReason.INSUFFICIENT_AVAILABLE_CASH,
                    f"需要冻结 {need}，可用 {account.cash_available}",
                )
            account.cash_available -= need
            account.cash_frozen += need
            return need
        # SELL：冻结持仓
        avail = account.position_available.get(symbol, ZERO)
        if avail < qty:
            raise OrderRejected(
                RejectReason.INSUFFICIENT_AVAILABLE_POSITION,
                f"需要冻结 {qty} {symbol}，可用 {avail}",
            )
        account.position_available[symbol] = avail - qty
        account.position_frozen[symbol] = (
            account.position_frozen.get(symbol, ZERO) + qty
        )
        return ZERO

    def release_cancel(
        self,
        account: Account,
        symbol: str,
        side: Side,
        remaining_qty: Decimal,
        freeze_price: Decimal,
    ) -> None:
        """撤单：把剩余冻结原路释放回可用侧。"""
        if remaining_qty <= 0:
            return
        if side is Side.BUY:
            amount = remaining_qty * freeze_price
            if account.cash_frozen < amount:  # 防御性不变式校验
                raise RuntimeError("现金冻结余额不足以释放（账务不一致）")
            account.cash_frozen -= amount
            account.cash_available += amount
        else:
            frozen = account.position_frozen.get(symbol, ZERO)
            if frozen < remaining_qty:
                raise RuntimeError("持仓冻结余额不足以释放（账务不一致）")
            account.position_frozen[symbol] = frozen - remaining_qty
            account.position_available[symbol] = (
                account.position_available.get(symbol, ZERO) + remaining_qty
            )

    # ---- 成交记账 -------------------------------------------------------
    def apply_fill(self, fill: Fill) -> None:
        """一笔成交同时记买卖双方账：
        买方：按冻结价解冻成交部分，成交额划给卖方、价差退回可用，
              可用持仓 += 成交量；
        卖方：冻结持仓 -= 成交量，可用资金 += 成交额。
        """
        notional = fill.qty * fill.price
        buyer_freeze_price = (
            fill.taker_freeze_price
            if fill.side is Side.BUY
            else fill.maker_freeze_price
        )
        buyer = self.require(
            fill.taker_account if fill.side is Side.BUY else fill.maker_account
        )
        seller = self.require(
            fill.maker_account if fill.side is Side.BUY else fill.taker_account
        )

        # 买方成交部分按“下单冻结价”整块解冻：成交额划给卖方，
        # 冻结价与成交价之间的价差（价格保护盈余）退回买方可用资金。
        frozen_chunk = fill.qty * buyer_freeze_price
        if buyer.cash_frozen < frozen_chunk:
            raise RuntimeError("买方冻结资金不足以成交（账务不一致）")
        if seller.position_frozen.get(fill.symbol, ZERO) < fill.qty:
            raise RuntimeError("卖方冻结持仓不足以成交（账务不一致）")

        buyer.cash_frozen -= frozen_chunk
        buyer.cash_available += frozen_chunk - notional
        buyer.position_available[fill.symbol] = (
            buyer.position_available.get(fill.symbol, ZERO) + fill.qty
        )

        seller.position_frozen[fill.symbol] = (
            seller.position_frozen.get(fill.symbol, ZERO) - fill.qty
        )
        seller.cash_available += notional
