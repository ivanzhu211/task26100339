"""性能验收测试：10 万账户、累计 100 万条信号。

运行: python3 -m pytest tests/test_performance.py -q -s
"""

from __future__ import annotations

import random
import time
from datetime import date, timedelta

import pytest

from leadscope_momentum import Impact, MomentumEngine

ACCOUNT_COUNT = 100_000
SIGNAL_COUNT = 1_000_000
HISTORY_DAYS = 1095  # 信号发生日期分布在约 3 年内，另含少量未来日期

BASE_DATE = date(2026, 1, 1)


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    """构造 100 万条信号的数据文件并建立动量状态。"""
    db_path = tmp_path_factory.mktemp("perf") / "momentum.db"
    eng = MomentumEngine(db_path)
    rng = random.Random(42)
    base = BASE_DATE.toordinal()
    rows = [
        (
            rng.randint(1, ACCOUNT_COUNT),
            base - rng.randint(0, HISTORY_DAYS) + (30 if rng.random() < 0.01 else 0),
            rng.choice((3, 1, -1)),
        )
        for _ in range(SIGNAL_COUNT)
    ]
    store = eng._store
    with store._conn:
        store._conn.executemany(
            "INSERT INTO signals(account_id, event_day, weight) VALUES(?, ?, ?)",
            rows,
        )
    # 首次查询触发全量建状态（模拟系统停用很久后重启的冷启动）
    t0 = time.perf_counter()
    eng.top_accounts(BASE_DATE)
    cold = time.perf_counter() - t0
    print(f"\n[setup] 100 万条信号冷启动建状态: {cold * 1000:.0f} ms")
    yield eng
    eng.close()


def _time(fn, repeat=1):
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def test_top_board_under_100ms(engine) -> None:
    elapsed = _time(lambda: engine.top_accounts(BASE_DATE), repeat=20)
    print(f"\n[perf] 看动量榜: {elapsed * 1000:.2f} ms")
    assert elapsed < 0.1


def test_single_account_under_100ms(engine) -> None:
    elapsed = _time(lambda: engine.momentum(12345, BASE_DATE), repeat=20)
    print(f"\n[perf] 查单个账户: {elapsed * 1000:.2f} ms")
    assert elapsed < 0.1


def test_single_write_under_10ms(engine) -> None:
    sid = engine.add_signal(999, BASE_DATE, Impact.POSITIVE, on_date=BASE_DATE)
    add_t = _time(
        lambda: engine.add_signal(999, BASE_DATE, Impact.NEUTRAL, on_date=BASE_DATE),
        repeat=5,
    )
    upd_t = _time(
        lambda: engine.update_signal(sid, on_date=BASE_DATE, impact=Impact.NEGATIVE),
        repeat=5,
    )
    del_t = _time(lambda: engine.delete_signal(sid + 1, on_date=BASE_DATE), repeat=1)
    print(f"\n[perf] 录入: {add_t * 1000:.2f} ms, 修改: {upd_t * 1000:.2f} ms, "
          f"删除: {del_t * 1000:.2f} ms")
    assert add_t < 0.01 and upd_t < 0.01 and del_t < 0.01


def test_next_day_first_query_under_1s(engine) -> None:
    next_day = BASE_DATE + timedelta(days=1)
    t0 = time.perf_counter()
    engine.top_accounts(next_day)
    elapsed = time.perf_counter() - t0
    print(f"\n[perf] 跨天后第一次查询: {elapsed * 1000:.2f} ms")
    assert elapsed < 1.0


def test_multi_month_jump_first_query_under_2s(engine) -> None:
    jumped = BASE_DATE + timedelta(days=1) + timedelta(days=180)
    t0 = time.perf_counter()
    engine.top_accounts(jumped)
    elapsed = time.perf_counter() - t0
    print(f"\n[perf] 跳过 180 天后第一次查询: {elapsed * 1000:.2f} ms")
    assert elapsed < 2.0
