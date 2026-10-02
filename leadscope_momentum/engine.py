"""账户动量引擎：对外暴露的 API。"""

from __future__ import annotations

import enum
from datetime import date
from pathlib import Path
from typing import Optional, Union

from .store import Store

PathLike = Union[str, Path]

_WINDOW_DAYS = 90
_DOUBLE_WINDOW_DAYS = 29


def _contribution(event_day: int, weight: int, day: int) -> tuple[int, int]:
    """一条信号在 ``day`` 当天的 (动量分, 是否在窗口内)。"""
    if event_day > day or day - event_day >= _WINDOW_DAYS:
        return 0, 0
    if day - event_day <= _DOUBLE_WINDOW_DAYS:
        return 2 * weight, 1
    return weight, 1


class Impact(enum.Enum):
    """信号的影响判断。"""

    POSITIVE = 3
    NEUTRAL = 1
    NEGATIVE = -1


class MomentumEngine:
    """账户动量引擎。

    - 当天日期由上层在每次调用时通过 ``on_date`` 传入，只允许原地或向后走。
    - 所有写操作（录入/修改/删除信号）按当前已推进到的日期立即计分。
    """

    def __init__(self, db_path: PathLike) -> None:
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "MomentumEngine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def add_signal(
        self,
        account_id: int,
        event_date: date,
        impact: Impact,
        on_date: date,
    ) -> int:
        """录入一条信号，返回信号 ID。"""
        self._store.advance_to(on_date.toordinal())
        event_day = event_date.toordinal()
        signal_id = self._store.insert_signal(account_id, event_day, impact.value)
        dm, da = _contribution(event_day, impact.value, on_date.toordinal())
        self._store.apply_signal_delta(account_id, dm, da)
        return signal_id

    def update_signal(
        self,
        signal_id: int,
        on_date: date,
        event_date: Optional[date] = None,
        impact: Optional[Impact] = None,
    ) -> None:
        """修改信号的发生日期和/或影响判断。"""
        self._store.advance_to(on_date.toordinal())
        old = self._store.get_signal(signal_id)
        if old is None:
            raise KeyError(f"信号不存在: {signal_id}")
        account_id, old_day, old_weight = old
        new_day = event_date.toordinal() if event_date is not None else old_day
        new_weight = impact.value if impact is not None else old_weight
        self._store.update_signal(signal_id, new_day, new_weight)
        day = on_date.toordinal()
        old_m, old_a = _contribution(old_day, old_weight, day)
        new_m, new_a = _contribution(new_day, new_weight, day)
        self._store.apply_signal_delta(
            account_id, new_m - old_m, new_a - old_a
        )

    def delete_signal(self, signal_id: int, on_date: date) -> None:
        """删除一条信号。"""
        self._store.advance_to(on_date.toordinal())
        old = self._store.get_signal(signal_id)
        if old is None:
            raise KeyError(f"信号不存在: {signal_id}")
        account_id, old_day, old_weight = old
        self._store.delete_signal(signal_id)
        old_m, old_a = _contribution(old_day, old_weight, on_date.toordinal())
        self._store.apply_signal_delta(account_id, -old_m, -old_a)

    def momentum(self, account_id: int, on_date: date) -> int:
        """查询单个账户在 ``on_date`` 当天的动量。"""
        self._store.advance_to(on_date.toordinal())
        return self._store.get_momentum(account_id)

    def top_accounts(self, on_date: date, limit: int = 10) -> list[tuple[int, int]]:
        """动量榜：返回 ``[(account_id, momentum), ...]``，按动量降序、编号升序。"""
        self._store.advance_to(on_date.toordinal())
        return self._store.get_top(limit)
