"""账户动量存储与计算。

设计要点：
- signals 表保存全部信号（发生日期存为 ordinal 整数天）。
- cache 表保存"当前日期"下每个账户的动量与窗口内信号数，
  由 meta 表记录 cache 对应的日期。
- 每次调用先传入当天日期；若日期前进，用三条集合化 SQL
  （新进入窗口 / 失去翻倍 / 离开窗口）增量刷新 cache。
- 增删改信号时立即按当前日期把该信号的贡献反映到 cache。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from .impact import Impact

WINDOW_DAYS = 90
DOUBLE_DAYS = 30


class MomentumStore:
    """账户动量库。所有方法都需要上层传入当天日期（date 只会前进）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MomentumStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 内部：schema ----

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id   INTEGER NOT NULL,
                    occurred_day INTEGER NOT NULL,  -- date.toordinal()
                    score        INTEGER NOT NULL CHECK (score IN (3, 1, -1))
                );
                CREATE INDEX IF NOT EXISTS idx_signals_occurred
                    ON signals (occurred_day, account_id, score);

                CREATE TABLE IF NOT EXISTS momentum_cache (
                    account_id   INTEGER PRIMARY KEY,
                    momentum     INTEGER NOT NULL,
                    active_count INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cache_rank
                    ON momentum_cache (momentum DESC, account_id ASC);

                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )

    # ---- 内部：日期推进 ----

    @staticmethod
    def _day(d: date) -> int:
        return d.toordinal()

    def _last_day(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'last_day'"
        ).fetchone()
        return None if row is None else int(row[0])

    def _set_last_day(self, day: int) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_day', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (day,),
        )

    def _ensure_day(self, today: date) -> int:
        """把 cache 推进到 today；返回 today 的 ordinal。调用方须持有事务。"""
        t = self._day(today)
        last = self._last_day()
        if last is None:
            self._set_last_day(t)
            return t
        if t < last:
            raise ValueError(
                f"today {today} is before last known day {date.fromordinal(last)}"
            )
        if t > last:
            self._advance(last, t)
        return t

    def _advance(self, last: int, t: int) -> None:
        """把 cache 从 last 天增量推进到 t 天（集合化 SQL，一次完成跨天/跨月）。"""
        l29, l89 = last - (DOUBLE_DAYS - 1), last - (WINDOW_DAYS - 1)
        t29, t89 = t - (DOUBLE_DAYS - 1), t - (WINDOW_DAYS - 1)
        self._conn.execute(
            """
            WITH deltas AS (
                -- 1) 在 (last, t] 内发生、且 t 当天仍在窗口内：按 t 天计分进入
                SELECT account_id,
                       SUM(CASE WHEN occurred_day >= :t29
                                THEN 2 * score ELSE score END) AS dm,
                       COUNT(*) AS da
                FROM signals
                WHERE occurred_day > :last
                  AND occurred_day <= :t
                  AND occurred_day >= :t89
                GROUP BY account_id
                UNION ALL
                -- 2) 两天都在窗口内、但跨过了 30 天翻倍线：失去翻倍部分
                SELECT account_id, -SUM(score), 0
                FROM signals
                WHERE occurred_day >= :t89
                  AND occurred_day <= :last
                  AND occurred_day >= :l29
                  AND occurred_day < :t29
                GROUP BY account_id
                UNION ALL
                -- 3) last 天在窗口内（occ <= last）、t 天已出窗口：按 last 天计分整体扣除
                SELECT account_id,
                       -SUM(CASE WHEN occurred_day >= :l29
                                 THEN 2 * score ELSE score END),
                       -COUNT(*)
                FROM signals
                WHERE occurred_day >= :l89
                  AND occurred_day <= :last
                  AND occurred_day < :t89
                GROUP BY account_id
            )
            INSERT INTO momentum_cache (account_id, momentum, active_count)
            SELECT account_id, SUM(dm), SUM(da) FROM deltas GROUP BY account_id
            ON CONFLICT(account_id) DO UPDATE SET
                momentum = momentum + excluded.momentum,
                active_count = active_count + excluded.active_count
            """,
            {"last": last, "t": t, "l29": l29, "l89": l89, "t29": t29, "t89": t89},
        )
        self._conn.execute("DELETE FROM momentum_cache WHERE active_count <= 0")
        self._set_last_day(t)

    def _recompute_all(self, today: date) -> None:
        """从 signals 全量重建 cache（内部工具，供批量导数/校验使用）。"""
        t = self._day(today)
        t29, t89 = t - (DOUBLE_DAYS - 1), t - (WINDOW_DAYS - 1)
        with self._conn:
            self._conn.execute("DELETE FROM momentum_cache")
            self._conn.execute(
                """
                INSERT INTO momentum_cache (account_id, momentum, active_count)
                SELECT account_id,
                       SUM(CASE WHEN occurred_day >= :t29
                                THEN 2 * score ELSE score END),
                       COUNT(*)
                FROM signals
                WHERE occurred_day >= :t89 AND occurred_day <= :t
                GROUP BY account_id
                """,
                {"t": t, "t29": t29, "t89": t89},
            )
            self._set_last_day(t)

    def add_signal(
        self, today: date, account_id: int, occurred_on: date, impact: Impact | int | str
    ) -> int:
        """登记一条信号，返回 signal_id。"""
        score = int(Impact.coerce(impact))
        occ = self._day(occurred_on)
        with self._conn:
            t = self._ensure_day(today)
            cur = self._conn.execute(
                "INSERT INTO signals (account_id, occurred_day, score) "
                "VALUES (?, ?, ?)",
                (account_id, occ, score),
            )
            signal_id = int(cur.lastrowid)
            self._apply_contribution(account_id, occ, score, t, sign=+1)
        return signal_id

    def update_signal(
        self,
        today: date,
        signal_id: int,
        *,
        occurred_on: date | None = None,
        impact: Impact | int | str | None = None,
    ) -> None:
        """修改信号的发生日期和/或影响判断。"""
        with self._conn:
            t = self._ensure_day(today)
            row = self._conn.execute(
                "SELECT account_id, occurred_day, score FROM signals WHERE id = ?",
                (signal_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"signal {signal_id} not found")
            account_id, old_occ, old_score = int(row[0]), int(row[1]), int(row[2])
            new_occ = old_occ if occurred_on is None else self._day(occurred_on)
            new_score = old_score if impact is None else int(Impact.coerce(impact))
            self._conn.execute(
                "UPDATE signals SET occurred_day = ?, score = ? WHERE id = ?",
                (new_occ, new_score, signal_id),
            )
            self._apply_contribution(account_id, old_occ, old_score, t, sign=-1)
            self._apply_contribution(account_id, new_occ, new_score, t, sign=+1)

    def delete_signal(self, today: date, signal_id: int) -> None:
        """删除一条信号。"""
        with self._conn:
            t = self._ensure_day(today)
            row = self._conn.execute(
                "SELECT account_id, occurred_day, score FROM signals WHERE id = ?",
                (signal_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"signal {signal_id} not found")
            account_id, occ, score = int(row[0]), int(row[1]), int(row[2])
            self._conn.execute("DELETE FROM signals WHERE id = ?", (signal_id,))
            self._apply_contribution(account_id, occ, score, t, sign=-1)

    # ---- 内部：单条信号对 cache 的即时贡献 ----

    @staticmethod
    def _contribution(occurred_day: int, score: int, t: int) -> tuple[int, int]:
        """返回 (动量贡献, 窗口内计数)，t 为当前天。"""
        if occurred_day > t or occurred_day < t - (WINDOW_DAYS - 1):
            return 0, 0
        if occurred_day >= t - (DOUBLE_DAYS - 1):
            return 2 * score, 1
        return score, 1

    def _apply_contribution(
        self, account_id: int, occurred_day: int, score: int, t: int, sign: int
    ) -> None:
        dm, da = self._contribution(occurred_day, score, t)
        if dm == 0 and da == 0:
            return
        self._conn.execute(
            "INSERT INTO momentum_cache (account_id, momentum, active_count) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "momentum = momentum + excluded.momentum, "
            "active_count = active_count + excluded.active_count",
            (account_id, sign * dm, sign * da),
        )

    def momentum(self, today: date, account_id: int) -> int:
        """查询单个账户当天动量。"""
        with self._conn:
            self._ensure_day(today)
            row = self._conn.execute(
                "SELECT momentum FROM momentum_cache WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return 0 if row is None else int(row[0])

    def leaderboard(self, today: date, limit: int = 10) -> list[tuple[int, int]]:
        """当天动量榜：[(account_id, momentum), ...] 按动量降序、编号升序。"""
        with self._conn:
            self._ensure_day(today)
            rows = self._conn.execute(
                "SELECT account_id, momentum FROM momentum_cache "
                "WHERE active_count > 0 "
                "ORDER BY momentum DESC, account_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(int(a), int(m)) for a, m in rows]
