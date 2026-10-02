"""SQLite 存储层：schema、信号 CRUD、按日期推进的增量状态维护。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]

# 动量窗口参数
WINDOW_DAYS = 90          # 信号计入动量的总窗口（含当天，两头都算）
DOUBLE_WINDOW_DAYS = 29   # 发生日期距当天不超过该天数时分数翻倍


class Store:
    """封装 SQLite 连接与所有 SQL 操作。"""

    def __init__(self, db_path: PathLike) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
                signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                event_day INTEGER NOT NULL,
                weight    INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_event_day
                ON signals(event_day);
            CREATE TABLE IF NOT EXISTS account_state (
                account_id   INTEGER PRIMARY KEY,
                momentum     INTEGER NOT NULL,
                active_count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_state_rank
                ON account_state(momentum DESC, account_id ASC);
            """
        )
        self._conn.commit()

    # ---- 元信息 ----

    def get_current_day(self) -> Optional[int]:
        """返回已推进到的日期（ordinal），未初始化时返回 None。"""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'current_day'"
        ).fetchone()
        return int(row[0]) if row else None

    def _set_current_day(self, day: int) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('current_day', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(day),),
        )

    # ---- 信号 CRUD ----

    def insert_signal(self, account_id: int, event_day: int, weight: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO signals(account_id, event_day, weight) VALUES(?, ?, ?)",
            (account_id, event_day, weight),
        )
        return int(cur.lastrowid)

    def get_signal(self, signal_id: int) -> Optional[tuple[int, int, int]]:
        """返回 (account_id, event_day, weight)，不存在时返回 None。"""
        row = self._conn.execute(
            "SELECT account_id, event_day, weight FROM signals WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
        return (int(row[0]), int(row[1]), int(row[2])) if row else None

    def update_signal(self, signal_id: int, event_day: int, weight: int) -> None:
        self._conn.execute(
            "UPDATE signals SET event_day = ?, weight = ? WHERE signal_id = ?",
            (event_day, weight, signal_id),
        )

    def delete_signal(self, signal_id: int) -> None:
        self._conn.execute("DELETE FROM signals WHERE signal_id = ?", (signal_id,))

    # ---- 动量状态 ----

    def advance_to(self, day: int) -> None:
        """把内部日期推进到 ``day``（只允许原地或向后），增量刷新账户动量状态。"""
        current = self.get_current_day()
        if current is None:
            # 首次推进：若已有信号（异常恢复等场景），按当天窗口全量建立状态
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO account_state(account_id, momentum, active_count)
                    SELECT account_id,
                           SUM(CASE WHEN :day - event_day <= :dbl
                                    THEN 2 * weight ELSE weight END),
                           COUNT(*)
                    FROM signals
                    WHERE event_day BETWEEN :day - :win + 1 AND :day
                    GROUP BY account_id
                    """,
                    {"day": day, "win": WINDOW_DAYS, "dbl": DOUBLE_WINDOW_DAYS},
                )
                self._set_current_day(day)
            return
        if day < current:
            raise ValueError(f"日期不能回退: 当前 {current}, 请求 {day}")
        if day == current:
            return
        old, new = current, day
        rows = self._conn.execute(
            """
            SELECT account_id,
                   SUM(CASE WHEN event_day > :new OR :new - event_day >= :win
                            THEN 0
                            WHEN :new - event_day <= :dbl THEN 2 * weight
                            ELSE weight END
                     - CASE WHEN event_day > :old OR :old - event_day >= :win
                            THEN 0
                            WHEN :old - event_day <= :dbl THEN 2 * weight
                            ELSE weight END) AS dm,
                   SUM(CASE WHEN event_day BETWEEN :new - :win + 1 AND :new
                            THEN 1 ELSE 0 END
                     - CASE WHEN event_day BETWEEN :old - :win + 1 AND :old
                            THEN 1 ELSE 0 END) AS da
            FROM signals
            WHERE event_day BETWEEN :old - :win + 1 AND :new
            GROUP BY account_id
            """,
            {"old": old, "new": new, "win": WINDOW_DAYS, "dbl": DOUBLE_WINDOW_DAYS},
        ).fetchall()
        with self._conn:
            self._conn.executemany(
                "INSERT INTO account_state(account_id, momentum, active_count) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET "
                "momentum = momentum + excluded.momentum, "
                "active_count = active_count + excluded.active_count",
                [(int(aid), int(dm), int(da)) for aid, dm, da in rows],
            )
            self._conn.execute(
                "DELETE FROM account_state WHERE momentum = 0 AND active_count = 0"
            )
            self._set_current_day(day)

    def apply_signal_delta(
        self, account_id: int, momentum_delta: int, active_delta: int
    ) -> None:
        """把一条信号的计分变化应用到账户状态表。"""
        with self._conn:
            self._conn.execute(
                "INSERT INTO account_state(account_id, momentum, active_count) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET "
                "momentum = momentum + excluded.momentum, "
                "active_count = active_count + excluded.active_count",
                (account_id, momentum_delta, active_delta),
            )
            self._conn.execute(
                "DELETE FROM account_state WHERE momentum = 0 AND active_count = 0"
            )

    def get_momentum(self, account_id: int) -> int:
        row = self._conn.execute(
            "SELECT momentum FROM account_state WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def get_top(self, limit: int) -> list[tuple[int, int]]:
        rows = self._conn.execute(
            "SELECT account_id, momentum FROM account_state "
            "WHERE active_count > 0 "
            "ORDER BY momentum DESC, account_id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(int(aid), int(m)) for aid, m in rows]
