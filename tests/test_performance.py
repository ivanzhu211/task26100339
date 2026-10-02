"""性能验收：10 万账户、100 万信号规模下的实测耗时。"""

from __future__ import annotations

import random
import time
from datetime import date, timedelta

import pytest

from leadscope_momentum import MomentumStore

ACCOUNTS = 100_000
SIGNALS = 1_000_000
SPAN_DAYS = 400  # 信号发生日期分布在过去 400 天到未来 10 天

LIMIT_LEADERBOARD = 0.100   # 看榜 < 100ms
LIMIT_MOMENTUM = 0.100      # 查单个账户 < 100ms
LIMIT_WRITE = 0.010         # 单条写入 < 10ms
LIMIT_NEXT_DAY = 1.0        # 跨天后第一次查询 < 1s
LIMIT_BIG_JUMP = 2.0        # 跳过几个月后第一次查询 < 2s


def _bulk_load(store: MomentumStore, today: date, rng: random.Random) -> list[tuple]:
    """绕过逐条 API 直接批量导数，再用全量重建建立缓存。"""
    rows = []
    base = today.toordinal()
    for i in range(SIGNALS):
        rows.append((
            i + 1,
            rng.randint(1, ACCOUNTS),
            base - SPAN_DAYS + rng.randint(0, SPAN_DAYS + 10),
            rng.choice((3, 1, -1)),
        ))
    with store._conn:
        store._conn.executemany(
            "INSERT INTO signals (id, account_id, occurred_day, score) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
    store._recompute_all(today)
    return rows


def _oracle_top(rows: list[tuple], today: date, limit: int = 10) -> list[tuple[int, int]]:
    t = today.toordinal()
    totals: dict[int, int] = {}
    for _, account_id, occ, score in rows:
        if t - 89 <= occ <= t:
            totals[account_id] = totals.get(account_id, 0) + (
                2 * score if occ >= t - 29 else score
            )
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:limit]


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    rng = random.Random(26100339)
    today = date(2026, 10, 2)
    db_file = tmp_path_factory.mktemp("perf") / "momentum.db"
    store = MomentumStore(db_file)
    rows = _bulk_load(store, today, rng)
    yield store, rows, today
    store.close()


def test_scale_sanity(loaded) -> None:
    """规模自检 + 榜单与全量重算对照。"""
    store, rows, today = loaded
    assert store._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == SIGNALS
    assert store.leaderboard(today) == _oracle_top(rows, today)


def test_leaderboard_steady_state(loaded) -> None:
    store, _, today = loaded
    store.leaderboard(today)  # 预热
    samples = []
    for _ in range(20):
        start = time.perf_counter()
        store.leaderboard(today)
        samples.append(time.perf_counter() - start)
    worst = max(samples)
    print(f"\nleaderboard steady-state: worst {worst*1000:.2f}ms")
    assert worst < LIMIT_LEADERBOARD


def test_single_account_query(loaded) -> None:
    store, _, today = loaded
    rng = random.Random(7)
    worst = 0.0
    for _ in range(200):
        account_id = rng.randint(1, ACCOUNTS)
        start = time.perf_counter()
        store.momentum(today, account_id)
        worst = max(worst, time.perf_counter() - start)
    print(f"\nsingle-account query: worst {worst*1000:.2f}ms")
    assert worst < LIMIT_MOMENTUM


def test_single_write_latency(loaded) -> None:
    store, _, today = loaded
    rng = random.Random(11)
    worst = 0.0
    ids = []
    for _ in range(200):
        start = time.perf_counter()
        sid = store.add_signal(
            today, rng.randint(1, ACCOUNTS),
            today - timedelta(days=rng.randint(0, 100)),
            rng.choice(("positive", "neutral", "negative")),
        )
        worst = max(worst, time.perf_counter() - start)
        ids.append(sid)
    for sid in ids[:100]:
        start = time.perf_counter()
        store.update_signal(today, sid, impact="negative")
        worst = max(worst, time.perf_counter() - start)
    for sid in ids:
        start = time.perf_counter()
        store.delete_signal(today, sid)
        worst = max(worst, time.perf_counter() - start)
    print(f"\nsingle write (add/update/delete): worst {worst*1000:.2f}ms")
    assert worst < LIMIT_WRITE


def test_first_query_next_day(loaded) -> None:
    store, _, today = loaded
    next_day = today + timedelta(days=1)
    start = time.perf_counter()
    store.leaderboard(next_day)
    elapsed = time.perf_counter() - start
    print(f"\nfirst query next day: {elapsed*1000:.2f}ms")
    assert elapsed < LIMIT_NEXT_DAY


def test_first_query_after_months_jump(loaded) -> None:
    store, _, today = loaded
    jumped = today + timedelta(days=92)  # 系统停用三个月
    start = time.perf_counter()
    board = store.leaderboard(jumped)
    elapsed = time.perf_counter() - start
    print(f"\nfirst query after 92-day jump: {elapsed*1000:.2f}ms")
    assert elapsed < LIMIT_BIG_JUMP
    assert len(board) == 10  # 榜单仍然有效
