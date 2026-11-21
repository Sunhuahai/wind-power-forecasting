"""真实风电场的严格 as-of 读取；支持 NWP 多批次及观测修订。

所有时间参数必须带时区；源数据始终保持 UTC 时刻语义。
同一 NWP 来源/模式在每个目标时刻选截止时间前最新已到达版本。
"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import text


def _aware(*values: datetime) -> None:
    if any(v.tzinfo is None or v.utcoffset() is None for v in values):
        raise ValueError("Operational time must be timezone-aware")


def nwp_snapshot(conn, *, farm_id: int, source: str,
                 cutoff_at: datetime, valid_start: datetime,
                 valid_end: datetime) -> list[dict]:
    """仅返回 issue 与 receive 均不晚于 cutoff 的 NWP。

    NWP 可为原生小时级；此函数不伪造 15 分钟气象观测。
    """
    _aware(cutoff_at, valid_start, valid_end)
    if not source or valid_end <= valid_start:
        raise ValueError("source and positive valid time range are required")
    sql = text("""
        SELECT DISTINCT ON (valid_at)
               farm_id, source, issued_at, received_at, valid_at,
               u100, v100, u10, v10, temperature_k, pressure_pa, extra
          FROM operational_nwp
         WHERE farm_id=:farm_id AND source=:source
           AND valid_at >= :valid_start AND valid_at < :valid_end
           AND issued_at <= :cutoff_at AND received_at <= :cutoff_at
         ORDER BY valid_at, issued_at DESC, received_at DESC
    """)
    return [dict(r) for r in conn.execute(sql, dict(
        farm_id=farm_id, source=source,
        cutoff_at=cutoff_at, valid_start=valid_start,
        valid_end=valid_end)).mappings()]


def power_history_snapshot(conn, *, farm_id: int, source: str,
                           cutoff_at: datetime, history_start: datetime,
                           issue_at: datetime) -> list[dict]:
    """历史功率必须既已发生又已收到；不从目标日泄漏未来观测。"""
    _aware(cutoff_at, history_start, issue_at)
    if not source:
        raise ValueError("source is required for a deterministic power feed")
    if history_start >= issue_at or cutoff_at > issue_at:
        raise ValueError("Require history_start < issue_at and cutoff_at <= issue_at")
    sql = text("""
        SELECT DISTINCT ON (valid_at)
               farm_id, valid_at, source, received_at,
               actual_mw, available_mw, curtailed, quality
          FROM operational_power
         WHERE farm_id=:farm_id AND source=:source
           AND valid_at >= :history_start AND valid_at < :issue_at
           AND received_at <= :cutoff_at
         ORDER BY valid_at, received_at DESC, source
    """)
    return [dict(r) for r in conn.execute(sql, dict(
        farm_id=farm_id, source=source, cutoff_at=cutoff_at,
        history_start=history_start, issue_at=issue_at)).mappings()]
