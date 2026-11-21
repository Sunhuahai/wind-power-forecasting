"""生产场站的只读广西考核数据装载与审计存档。

只读取 operational_* / forecast_* 表，不访问公开数据集的归一化标签。
临时不对有豁免或缺失的日生成貌似正式的监管分数。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text
from windpower.db.core import engine
from windpower.evaluation.guangxi import (
    BEIJING, Forecast, Observation, business_day_targets, score_day,
)


def score_from_database(
    *,
    farm_id: int,
    target_date: date,
    product: str,
    rule_version: str = "GX_WIND_PUBLIC_2025",
    issuance_id: str | None = None,
    model_version: str | None = None,
    series_id: str | None = None,
    as_of: datetime | None = None,
    power_source: str = "scada",
    save: bool = False,
) -> dict:
    """按场站日计算，写入历史评分表须显式 save=True。

    day_ahead 必须指定已冻结的单一发行 ID；
    ultra_short 必须指定 model_version + series_id，避免混合多模型/重跑。
    """
    if product == "day_ahead" and not issuance_id:
        raise ValueError("Day-ahead requires --issuance-id for an immutable published version")
    if product == "ultra_short" and (not model_version or not series_id):
        raise ValueError("Ultra-short requires --model-version and --series-id")
    if not power_source:
        raise ValueError("power_source must identify the authorized reference feed")
    cutoff = as_of or datetime.now(timezone.utc)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("as_of must include an explicit timezone")
    cutoff = cutoff.astimezone(timezone.utc)
    targets = business_day_targets(target_date)
    start = targets[0]
    end = targets[-1] + timedelta(minutes=15)
    selector = (
        "AND i.issuance_id = :issuance_id" if product == "day_ahead"
        else "AND i.model_version = :model_version AND i.series_id = :series_id"
    )
    params = dict(farm_id=farm_id, start=start, end=end, as_of=cutoff,
                  product=product, issuance_id=issuance_id,
                  model_version=model_version, series_id=series_id,
                  power_source=power_source)
    with engine().connect() as c:
        capacity_rows = c.execute(text("""
            SELECT capacity_mw, effective_at, source
              FROM farm_capacity_history
             WHERE farm_id=:farm_id AND effective_at<=:start
               AND (effective_to IS NULL OR effective_to>:start)
             ORDER BY effective_at DESC LIMIT 2
        """), params).mappings().all()
        if len(capacity_rows) != 1:
            raise ValueError("Capacity history is absent or ambiguous at business-day start")
        capacity = float(capacity_rows[0]["capacity_mw"])
        # 保留到达时间作为版本；同一目标取考核回放截止前的最新观测修订。
        power_rows = c.execute(text("""
            SELECT DISTINCT ON (valid_at) valid_at, actual_mw, available_mw,
                   curtailed, received_at, source
              FROM operational_power
             WHERE farm_id=:farm_id AND source=:power_source
               AND valid_at>=:start AND valid_at<:end
               AND received_at<=:as_of
             ORDER BY valid_at, received_at DESC, source
        """), params).mappings().all()
        forecast_rows = c.execute(text("""
            SELECT i.issued_at, v.valid_at, v.predicted_mw
              FROM forecast_values v
              JOIN forecast_issuances i ON v.issuance_id=i.issuance_id
             WHERE i.farm_id=:farm_id AND i.product=:product
               AND v.valid_at>=:start AND v.valid_at<:end
               AND i.created_at<=:as_of AND i.issued_at<=:as_of
               """ + selector), params).mappings().all()
        # 审核通过才可豁免；审批状态未知时不能自行推断。
        event_rows = c.execute(text("""
            SELECT start_at, end_at
              FROM operational_events
             WHERE farm_id=:farm_id
               AND event_type='assessment_exemption'
               AND approval_status='approved'
               AND start_at<:end AND end_at>:start
        """), params).mappings().all()

    exempt_times = {
        t for t in targets
        if any(row["start_at"] <= t < row["end_at"] for row in event_rows)
    }
    observations = [
        Observation(
            valid_at=r["valid_at"], actual_mw=r["actual_mw"],
            available_mw=r["available_mw"], curtailed=bool(r["curtailed"]),
            approved_exemption=r["valid_at"].astimezone(timezone.utc) in exempt_times,
        )
        for r in power_rows
    ]
    forecasts = [
        Forecast(issued_at=r["issued_at"], valid_at=r["valid_at"],
                 predicted_mw=r["predicted_mw"])
        for r in forecast_rows
    ]
    score = score_day(target_date, product, capacity, observations, forecasts,
                      rule_version=rule_version)
    score.update(
        farm_id=farm_id,
        model_version=model_version if product == "ultra_short" else None,
        series_id=series_id if product == "ultra_short" else None,
        issuance_id=issuance_id if product == "day_ahead" else None,
        observation_as_of=cutoff.isoformat(),
        capacity_source=capacity_rows[0]["source"],
        reference_source=power_source,
        measured_reference_rows=len(power_rows),
        measured_forecast_rows=len(forecast_rows),
    )
    if save:
        # 研究回放默认仅查询；显式保存的每条结果只追加，不覆盖。
        evaluation_id = str(uuid4())
        with engine().begin() as c:
            c.execute(text("""
                INSERT INTO gx_daily_evaluations
                (evaluation_id, farm_id, target_date, product, policy_code,
                 model_version, series_id, issuance_id, evaluated_at,
                 status, scored_targets, accuracy, mae_mw, rmse_mw, detail)
                VALUES (:evaluation_id, :farm_id, :target_date, :product, :policy_code,
                        :model_version, :series_id, :issuance_id, :evaluated_at,
                        :status, :scored_targets, :accuracy, :mae_mw, :rmse_mw,
                        CAST(:detail AS jsonb))
            """), {
                "evaluation_id": evaluation_id, "farm_id": farm_id,
                "target_date": target_date, "product": product,
                "policy_code": rule_version, "model_version": model_version,
                "series_id": series_id, "issuance_id": issuance_id,
                "evaluated_at": datetime.now(timezone.utc),
                "status": score["status"],
                "scored_targets": score["scored_target_count"],
                "accuracy": score["GX_policy_accuracy"],
                "mae_mw": score["MAE_mw"],
                "rmse_mw": score["RMSE_mw"],
                "detail": __import__("json").dumps(score, ensure_ascii=False),
            })
        score["evaluation_id"] = evaluation_id
    return score
