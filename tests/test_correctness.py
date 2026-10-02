"""正确性验收测试：随机操作序列 vs 全量重算。"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from leadscope_momentum import Impact, MomentumEngine

WINDOW_DAYS = 90
DOUBLE_WINDOW_DAYS = 29


class BruteForce:
    """用全部信号从头算一遍的参考实现。"""

    def __init__(self) -> None:
        # signal_id -> (account_id, event_day, weight)
        self.signals: dict[int, tuple[int, int, int]] = {}

    def add(self, signal_id: int, account_id: int, event_day: int, weight: int) -> None:
        self.signals[signal_id] = (account_id, event_day, weight)

    def update(self, signal_id: int, event_day: int, weight: int) -> None:
        account_id = self.signals[signal_id][0]
        self.signals[signal_id] = (account_id, event_day, weight)

    def delete(self, signal_id: int) -> None:
        del self.signals[signal_id]

    def _in_window(self, event_day: int, day: int) -> bool:
        return event_day <= day and day - event_day < WINDOW_DAYS

    def momentum(self, account_id: int, day: int) -> int:
        total = 0
        for aid, event_day, weight in self.signals.values():
            if aid == account_id and self._in_window(event_day, day):
                total += 2 * weight if day - event_day <= DOUBLE_WINDOW_DAYS else weight
        return total

    def top(self, day: int, limit: int = 10) -> list[tuple[int, int]]:
        per: dict[int, int] = {}
        active: set[int] = set()
        for aid, event_day, weight in self.signals.values():
            if self._in_window(event_day, day):
                active.add(aid)
                per[aid] = per.get(aid, 0) + (
                    2 * weight if day - event_day <= DOUBLE_WINDOW_DAYS else weight
                )
        ranked = sorted(active, key=lambda a: (-per[a], a))
        return [(a, per[a]) for a in ranked[:limit]]


ACCOUNT_IDS = list(range(1, 51))
IMPACTS = [Impact.POSITIVE, Impact.NEUTRAL, Impact.NEGATIVE]


def check_all(engine: MomentumEngine, ref: BruteForce, day: date) -> None:
    assert engine.top_accounts(day, limit=10) == ref.top(day.toordinal(), limit=10)
    for account_id in ACCOUNT_IDS:
        assert engine.momentum(account_id, day) == ref.momentum(
            account_id, day.toordinal()
        ), f"account {account_id} @ {day}"


def test_randomized_operations_match_brute_force(tmp_path) -> None:
    rng = random.Random(20261002)
    db_path = tmp_path / "momentum.db"
    engine = MomentumEngine(db_path)
    ref = BruteForce()

    day = date(2026, 1, 1)
    live_ids: list[int] = []
    reopened = False
    jumped = False

    for step in range(800):
        # 中段关闭再重新打开数据文件
        if step == 400 and not reopened:
            engine.close()
            engine = MomentumEngine(db_path)
            reopened = True
            check_all(engine, ref, day)
            continue

        # 一次跳过几个月：先预录一批落在跳跃区间内的未来信号，
        # 保证有信号在跳过期间从进入窗口到离开窗口整个走完
        if step == 600 and not jumped:
            for account_id in rng.sample(ACCOUNT_IDS, 10):
                event_date = day + timedelta(days=rng.randint(5, 40))
                impact = rng.choice(IMPACTS)
                sid = engine.add_signal(account_id, event_date, impact, on_date=day)
                ref.add(sid, account_id, event_date.toordinal(), impact.value)
                live_ids.append(sid)
            day = day + timedelta(days=150)  # 150 > 90 + 40，信号完整走完窗口
            jumped = True
            check_all(engine, ref, day)
            continue

        op = rng.random()
        if op < 0.45 or not live_ids:
            # 录入：补录过去日期 / 当天 / 预录未来日期
            account_id = rng.choice(ACCOUNT_IDS)
            offset = rng.randint(-85, 60)
            event_date = day + timedelta(days=offset)
            impact = rng.choice(IMPACTS)
            sid = engine.add_signal(account_id, event_date, impact, on_date=day)
            ref.add(sid, account_id, event_date.toordinal(), impact.value)
            live_ids.append(sid)
        elif op < 0.65:
            # 改发生日期
            sid = rng.choice(live_ids)
            event_date = day + timedelta(days=rng.randint(-85, 60))
            engine.update_signal(sid, on_date=day, event_date=event_date)
            _, _, weight = ref.signals[sid]
            ref.update(sid, event_date.toordinal(), weight)
        elif op < 0.80:
            # 改影响判断
            sid = rng.choice(live_ids)
            impact = rng.choice(IMPACTS)
            engine.update_signal(sid, on_date=day, impact=impact)
            _, event_day, _ = ref.signals[sid]
            ref.update(sid, event_day, impact.value)
        elif op < 0.90:
            # 删除
            sid = live_ids.pop(rng.randrange(len(live_ids)))
            engine.delete_signal(sid, on_date=day)
            ref.delete(sid)
        else:
            # 日期前进 1~10 天
            day = day + timedelta(days=rng.randint(1, 10))

        check_all(engine, ref, day)

    # 最终再重启一次，确认持久化状态一致
    engine.close()
    engine = MomentumEngine(db_path)
    check_all(engine, ref, day)
    engine.close()
    assert jumped and reopened


def test_date_cannot_move_backwards(tmp_path) -> None:
    engine = MomentumEngine(tmp_path / "m.db")
    engine.momentum(1, date(2026, 2, 1))
    with pytest.raises(ValueError):
        engine.momentum(1, date(2026, 1, 1))
    engine.close()


def test_window_boundaries(tmp_path) -> None:
    engine = MomentumEngine(tmp_path / "m.db")
    d0 = date(2026, 3, 1)
    sid = engine.add_signal(7, d0, Impact.POSITIVE, on_date=d0)
    assert engine.momentum(7, d0) == 6                       # 当天翻倍
    assert engine.momentum(7, d0 + timedelta(days=29)) == 6  # 第 29 天仍翻倍
    assert engine.momentum(7, d0 + timedelta(days=30)) == 3  # 第 30 天不翻倍
    assert engine.momentum(7, d0 + timedelta(days=89)) == 3  # 第 89 天仍在窗口
    assert engine.momentum(7, d0 + timedelta(days=90)) == 0  # 第 90 天出窗口
    assert engine.top_accounts(d0 + timedelta(days=90)) == []
    engine.delete_signal(sid, on_date=d0 + timedelta(days=90))
    engine.close()


def test_zero_momentum_account_stays_on_board(tmp_path) -> None:
    """窗口内有信号但动量为 0 的账户仍应上榜。"""
    engine = MomentumEngine(tmp_path / "m.db")
    d0 = date(2026, 4, 1)
    engine.add_signal(3, d0, Impact.POSITIVE, on_date=d0)   # +3
    engine.add_signal(3, d0, Impact.NEGATIVE, on_date=d0)   # -1
    engine.add_signal(3, d0, Impact.NEGATIVE, on_date=d0)   # -1
    engine.add_signal(3, d0, Impact.NEGATIVE, on_date=d0)   # -1
    far = d0 + timedelta(days=60)  # 不再翻倍：3 - 1 - 1 - 1 = 0
    assert engine.momentum(3, far) == 0
    assert engine.top_accounts(far) == [(3, 0)]
    engine.close()
