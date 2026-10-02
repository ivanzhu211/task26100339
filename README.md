# LeadScope 账户动量模块

销售情报平台的账户动量模块，以库的形式交付，由上层服务直接调用，无接口层。
数据持久化在本地 SQLite 文件中，只依赖 Python 标准库。

## 用法

```python
from datetime import date
from leadscope_momentum import MomentumStore

store = MomentumStore("momentum.db")   # 本地 SQLite 文件
today = date.today()                    # 当天日期由上层每次调用时传入

# 登记 / 修改 / 删除信号（立即按当天计分）
sid = store.add_signal(today, account_id=42,
                       occurred_on=date(2026, 9, 20), impact="positive")
store.update_signal(today, sid, occurred_on=date(2026, 9, 25), impact="neutral")
store.delete_signal(today, sid)

# 查询
store.momentum(today, 42)        # -> int，单个账户当天动量
store.leaderboard(today)         # -> [(account_id, momentum), ...] 前 10 名
store.close()
```

- `impact` 接受 `"positive" / "neutral" / "negative"` 或 `Impact` 枚举，
  基础分分别为 3 / 1 / -1。
- 当天日期只许前进不许回退（回退会抛 `ValueError`）；两次调用间隔任意，
  进程重启后重新打开数据文件即可继续。
- 发生日期可以在过去（补录）或未来（预录，到达当天才计分）。

## 计分规则

- 账户在某天的动量 = 发生日期落在 `[当天-89, 当天]`（共 90 天，两头都算）
  的所有信号分数之和，可以为负数。
- 发生日期落在 `[当天-29, 当天]` 内的信号分数翻倍。
- 动量榜按动量降序、账户编号升序列出前 10 个窗口内至少有一条信号的账户。

## 设计

- `signals` 表保存全部信号（发生日期存为 ordinal 整数天），
  覆盖索引 `(occurred_day, account_id, score)` 支撑窗口聚合。
- `momentum_cache` 表保存"当前日期"下每个账户的动量与窗口内信号数，
  `meta` 表记录缓存对应的日期；增删改信号时立即把该信号的贡献写进缓存。
- 日期前进时用三条集合化 SQL 一次性增量刷新缓存（新进入窗口 /
  跨过 30 天翻倍线失去翻倍 / 离开 90 天窗口），跳过几个月也只执行一次，
  代价与经过的信号数成正比，而不是与天数成正比。
- 榜单查询走 `(momentum DESC, account_id ASC)` 索引，直接 `LIMIT 10`。

## 测试

```bash
python3 -m pytest
```

- `tests/test_correctness.py`：窗口/翻倍边界、未来信号生效、负动量上榜、
  日期回退报错；以及随机穿插补录、预录、改日期、改影响、删除、日期前进
  （含一次跳过 200 天，期间有信号完整走完窗口）和中途关闭重开数据文件的
  差分测试，每一步都与全量重算对照。
- `tests/test_performance.py`：10 万账户、100 万信号规模下实测
  看榜 / 查单账户 / 单条写入 / 跨天首查 / 跳过 92 天首查的耗时。
  本机实测约：看榜 0.02ms、查单账户 0.03ms、单条写入最差 6.5ms、
  跨天首查 42ms、跳过三个月首查 212ms。
