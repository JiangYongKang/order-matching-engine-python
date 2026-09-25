"""仅追加的本地事件日志（WAL），用于幂等记录与崩溃恢复。

- 每条事件 append 后 flush + fsync，保证“已响应即已落盘”；
- 进程重启后顺序重放事件即可重建账户、订单与订单簿结论；
- path=None 时为纯内存模式（测试用，不落盘、重启不保留）；
- 不依赖任何外部服务或真实凭据。

事件类型（见 EventType）：
  ACCOUNT_OPENED  开户（含初始资金/持仓）
  ORDER_ACCEPTED  订单通过风控并完成冻结后落盘（含冻结快照）
  ORDER_FILLED    一次成交（taker/maker 双方与成交价量）
  ORDER_CANCELED  撤单（含释放量）
  ORDER_REJECTED  风控拒单（含可区分原因；便于解释，不影响余额）
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

from app.enums import EventType, OrderStatus, OrderType, Side


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (Side, OrderType, OrderStatus, EventType)):
        return obj.value
    raise TypeError(f"不可序列化类型: {type(obj)!r}")


class EventLog:
    """JSON Lines 事件日志。"""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self._fh = None
        self._mem: list[dict] = []
        self._seq = 0

    def open(self) -> EventLog:
        if self.path is not None:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            # 追加模式打开；先用只读方式统计已有事件数作为起始序号。
            # 句柄需长期持有用于追加，故此处有意不使用 with（close 时关闭）。
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as fh:
                    self._seq = sum(1 for line in fh if line.strip())
            self._fh = open(  # noqa: SIM115
                self.path, "a", encoding="utf-8", buffering=1)
        else:
            self._seq = 0
        return self

    def append(self, event_type: EventType, payload: dict) -> dict:
        """落盘一条事件（同步 fsync），返回带序号的完整事件字典。"""
        event = {"seq": self._seq, "type": event_type.value, "payload": payload}
        self._seq += 1
        line = json.dumps(event, ensure_ascii=False, default=_default)
        self._mem.append(event)
        if self._fh is not None:
            self._fh.write(line + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        return event

    def read_all(self) -> list[dict]:
        """读取全部事件（重放用）。内存模式返回本进程已写事件。"""
        if self.path is None:
            return list(self._mem)
        events: list[dict] = []
        if not os.path.exists(self.path):
            return events
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "EventLog":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()
