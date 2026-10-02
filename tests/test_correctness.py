"""正确性验收：随机操作序列 + 全量重算对照。"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from leadscope_momentum import Impact, MomentumStore

WINDOW = 90
DOUBLE = 30


class Oracle:
    """暴力对照：保存全部信号，每次从头算。"""

    def __init__(self) -> None:
        self.signals: dict[int, tuple[int, int, int]] = {}  # id -> (account, day, score)

    def add(self, signal_id: int, account_id: int, day: int, score: int) -> None:
        self.signals[signal_id] = (account_id, day, score)

    def update(self, signal_id: int, day: int, score: int) -> None:
        account_id = self.signals[signal_id][0]
        self.signals[signal_id] = (account_id, day, score)

    def delete(self, signal_id: int) -> None:
        del self.signals[signal_id]

    def momentum(self, account_id: int, today: int) -> int:
        total = 0
        for acc, day, score in self.signals.values():
            if acc != account_id or not (today - (WINDOW - 1) <= day <= today):
                continue
            total += 2 * score if day >= today - (DOUBLE - 1) else score
        return total

    def leaderboard(self, today: int, limit: int = 10) -> list[tuple[int, int]]:
        active = {acc for acc, day, _ in self.signals.values()
                  if today - (WINDOW - 1) <= day <= today}
        ranked = sorted(((self.momentum(a, today), a) for a in active),
                        key=lambda x: (-x[0], x[1]))
        return [(a, m) for m, a in ranked[:limit]]


def test_window_and_double_boundaries() -> None:
    store = MomentumStore(":memory:")
    today = date(2026, 10, 2)
    # 恰好 89 天前：在窗口内；90 天前：不在
    store.add_signal(today, 1, today - timedelta(days=89), "positive")
    store.add_signal(today, 2, today - timedelta(days=90), "positive")
    # 恰好 29 天前：翻倍；30 天前：不翻倍
    store.add_signal(today, 3, today - timedelta(days=29), "positive")
    store.add_signal(today, 4, today - timedelta(days=30), "positive")
    assert store.momentum(today, 1) == 3
    assert store.momentum(today, 2) == 0
    assert store.momentum(today, 3) == 6
    assert store.momentum(today, 4) == 3
    # 中性和负面
    store.add_signal(today, 5, today, "neutral")
    store.add_signal(today, 6, today, "negative")
    assert store.momentum(today, 5) == 2
    assert store.momentum(today, 6) == -2
    store.close()


def test_future_signal_activates_on_its_day() -> None:
    store = MomentumStore(":memory:")
    d0 = date(2026, 10, 2)
    store.add_signal(d0, 1, d0 + timedelta(days=10), "positive")
    assert store.momentum(d0, 1) == 0
    assert store.leaderboard(d0) == []
    d1 = d0 + timedelta(days=10)
    assert store.momentum(d1, 1) == 6
    assert store.leaderboard(d1) == [(1, 6)]
    store.close()


def test_negative_momentum_on_board() -> None:
    store = MomentumStore(":memory:")
    today = date(2026, 10, 2)
    store.add_signal(today, 7, today, "negative")
    store.add_signal(today, 8, today, "negative")
    assert store.leaderboard(today) == [(7, -2), (8, -2)]
    store.close()


def test_date_cannot_go_backwards() -> None:
    store = MomentumStore(":memory:")
    today = date(2026, 10, 2)
    store.leaderboard(today)
    with pytest.raises(ValueError):
        store.leaderboard(today - timedelta(days=1))
    store.close()


def test_missing_signal_raises() -> None:
    store = MomentumStore(":memory:")
    today = date(2026, 10, 2)
    with pytest.raises(KeyError):
        store.update_signal(today, 999, impact="positive")
    with pytest.raises(KeyError):
        store.delete_signal(today, 999)
    store.close()


def test_randomized_differential(tmp_path) -> None:
    rng = random.Random(20261002)
    db_file = tmp_path / "momentum.db"
    store = MomentumStore(db_file)
    oracle = Oracle()

    today = date(2026, 1, 1)
    account_ids = list(range(1, 201))
    live_ids: list[int] = []
    impacts = [Impact.POSITIVE, Impact.NEUTRAL, Impact.NEGATIVE]

    def random_occurred() -> date:
        # 补录过去 / 当天 / 预录未来
        return today + timedelta(days=rng.randint(-120, 30))

    def check_all() -> None:
        t = today.toordinal()
        assert store.leaderboard(today) == oracle.leaderboard(t)
        for account_id in account_ids:
            assert store.momentum(today, account_id) == oracle.momentum(account_id, t)

    jump_at_step = 300
    jump_days = 200

    reopen_at = {120, 450}
    step = 0
    while step < 700:
        step += 1
        roll = rng.random()
        if step == jump_at_step:
            # 跳跃前预录一批信号，发生日期落在跳过区间内且离落点超过 90 天，
            # 保证它们在跳过的这段时间里完整地进入窗口又离开窗口。
            for _ in range(20):
                occ = today + timedelta(days=rng.randint(1, jump_days - WINDOW - 5))
                account_id = rng.choice(account_ids)
                impact = rng.choice(impacts)
                sid = store.add_signal(today, account_id, occ, impact)
                oracle.add(sid, account_id, occ.toordinal(), int(impact))
                live_ids.append(sid)
            today += timedelta(days=jump_days)  # 日期一下跳过几个月
        elif roll < 0.45:
            sid = store.add_signal(
                today, rng.choice(account_ids), random_occurred(), rng.choice(impacts)
            )
            row = store._conn.execute(
                "SELECT account_id, occurred_day, score FROM signals WHERE id = ?",
                (sid,),
            ).fetchone()
            oracle.add(sid, row[0], row[1], row[2])
            live_ids.append(sid)
        elif roll < 0.65 and live_ids:
            sid = rng.choice(live_ids)
            new_occ = random_occurred()
            new_impact = rng.choice(impacts)
            store.update_signal(today, sid, occurred_on=new_occ, impact=new_impact)
            oracle.update(sid, new_occ.toordinal(), int(new_impact))
        elif roll < 0.80 and live_ids:
            sid = live_ids.pop(rng.randrange(len(live_ids)))
            store.delete_signal(today, sid)
            oracle.delete(sid)
        else:
            today += timedelta(days=rng.randint(1, 5))
        if step in reopen_at:
            store.close()
            store = MomentumStore(db_file)  # 中途关闭再重新打开
        check_all()

    store.close()
