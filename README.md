# LeadScope 账户动量模块

销售情报平台的账户动量库，由上层服务直接调用（无接口层），数据持久化在本地 SQLite 文件中。

## 用法

```python
from datetime import date
from leadscope_momentum import MomentumEngine, Impact

engine = MomentumEngine("momentum.db")   # 本地 SQLite 文件

today = date.today()

# 录入信号（支持补录过去日期、预录未来日期），返回信号 ID
sid = engine.add_signal(account_id=1001, event_date=today,
                        impact=Impact.POSITIVE, on_date=today)

# 修改 / 删除信号，立即按新情况计分
engine.update_signal(sid, on_date=today, impact=Impact.NEUTRAL)
engine.delete_signal(sid, on_date=today)

# 动量榜：前 10 名 [(account_id, momentum), ...]
board = engine.top_accounts(today)

# 单个账户当天动量
m = engine.momentum(1001, today)

engine.close()
```

## 规则

- 动量窗口 90 天（含当天，两头都算）；正面 +3 / 中性 +1 / 负面 -1；
  发生日期距当天 29 天以内的信号分数翻倍；未来日期的信号到期才开始计分。
- 当天日期由上层每次调用时通过 `on_date` 传入，只允许原地或向后走；
  跨天、跨月由库内部增量刷新，进程重启后从 SQLite 状态继续。
- 榜单只含 90 天窗口内至少有一条信号的账户；动量相同按账户编号升序。

## 测试

```bash
python3 -m pytest tests/ -q -s
```

- `tests/test_correctness.py`：随机穿插补录/预录/改日期/改影响/删除/日期前进
  （含一次跨数月跳跃与中途关库重开），逐步与全量重算参考实现比对。
- `tests/test_performance.py`：10 万账户、100 万条信号下的时延验收。
