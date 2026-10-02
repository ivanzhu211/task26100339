"""信号的影响判断及其计分。"""

from __future__ import annotations

import enum


class Impact(enum.IntEnum):
    """信号影响判断，枚举值即基础分。"""

    POSITIVE = 3
    NEUTRAL = 1
    NEGATIVE = -1

    @classmethod
    def coerce(cls, value: "Impact | int | str") -> "Impact":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError:
                raise ValueError(f"unknown impact: {value!r}") from None
        return cls(value)
