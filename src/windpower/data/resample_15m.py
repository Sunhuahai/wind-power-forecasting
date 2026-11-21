"""10 分钟 SCADA 平均值 -> 15 分钟研究性代理功率的重采样。

规划 §6 的能量一致换算：
    P̂[a,b) = Σ_k overlap([a,b),[t_k,t_k+10m)) * P_k / (b-a)，b-a=15m。

阻塞说明（未确认前不得对真实数据启用）：
1. 原文件"统计时间=t"是否表示区间 [t, t+10min) 的平均，未经源系统确认；
   若实为区间终点标签或滑动平均，本公式必须重写，Agent 不得自行猜测。
2. 缺任一贡献区间的 15 分钟时段不输出、不按剩余样本重新归一化。
3. 输出恒为 research_only 代理标签，不是调度 15 分钟真值，无政策评价资格。
"""
from __future__ import annotations

from datetime import datetime, timedelta

STEP_10M = timedelta(minutes=10)
STEP_15M = timedelta(minutes=15)


def resample_10m_to_15m(
    records: list[tuple[datetime, float | None, int | str]],
    start: datetime,
    count: int,
    assume_interval_start: bool = True,
) -> list[dict]:
    """把 10 分钟平均记录重采样为 15 分钟代理区间。

    records: [(统计时间, 平均功率或 None, 行引用), ...]，时间升序、10 分钟网格。
    start/count: 15 分钟目标网格（时段起点）从 start 起共 count 个。
    返回的每个时段携带：proxy_kw、covered_minutes、贡献行引用与重叠分钟权重。
    贡献区间缺失或功率为 None 的时段直接缺席，不做补值或重新归一化。
    """
    if not assume_interval_start:
        raise ValueError(
            'Only the interval-start labelling assumption is implemented; '
            'interval-end or sliding-average semantics must be confirmed by the source system first')
    if start.minute % 15 or start.second or start.microsecond:
        raise ValueError('15m grid must start on a quarter-hour boundary')
    by_time: dict[datetime, tuple[float | None, int | str]] = {}
    for t, value, ref in records:
        if t.minute % 10 or t.second:
            raise ValueError(f'10m record off grid: {t}')
        if t in by_time:
            raise ValueError(f'Duplicate 10m record at {t}; select a source version first')
        by_time[t] = (value, ref)
    out = []
    for i in range(count):
        a = start + i * STEP_15M
        b = a + STEP_15M
        contributions, weight_sum, proxy = [], 0.0, 0.0
        # 起点回退到覆盖 a 的 10 分钟区间：[a,b) 可能切入更早开始的区间。
        k = a - timedelta(minutes=a.minute % 10)
        complete = True
        while k < b:
            minutes = int((min(b, k + STEP_10M) - max(a, k)).total_seconds() // 60)
            if minutes > 0:
                value, ref = by_time.get(k, (None, None))
                if value is None:
                    complete = False
                    break
                contributions.append({'row_ref': ref, 'observed_at': str(k), 'minutes': minutes})
                proxy += minutes * value
                weight_sum += minutes
            k += STEP_10M
        if not complete or weight_sum != 15:
            continue  # 缺贡献区间：时段缺席，不重归一化
        out.append({
            'valid_at': a, 'method': 'overlap_weighted_from_10m_interval_start',
            'proxy_kw': proxy / 15.0, 'covered_minutes': weight_sum,
            'contributing_rows': contributions,
            'research_only': True,
            'label_semantics_confirmed': False,
        })
    return out
