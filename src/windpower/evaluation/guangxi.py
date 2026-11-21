"""广西风电功率预测的研究性政策评分器。

输入必须是完整、可追溯的 15 分钟真实场站 MW 数据。
本模块只计算政策数学口径；不对缺报、豁免或月度费用做未经确认的推断。
源数据与模型选择不由本模块决定，既有 GEFCom2014 指标保持不变。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import ceil, isfinite, sqrt
from statistics import mean
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
STEP = timedelta(minutes=15)
POLICIES = {
    "GX_WIND_PUBLIC_2025": {
        "status": "published_rule",
        "thresholds": {"day_ahead": 0.60, "ultra_short": 0.65},
        "ultra_short_mode": "mean_then_daily_rmse",
        "ultra_short_reference": "actual_unless_curtailed",
    },
    "GX_WIND_DRAFT_202608": {
        "status": "consultation_draft_not_confirmed_effective",
        "thresholds": {"day_ahead": 0.65, "ultra_short": 0.75},
        "ultra_short_mode": "weighted_per_target_rmse_then_daily_mean",
        "ultra_short_reference": "available",
    },
}
# 草案四个小时权重比 4:3:2:1；小时内等权是待确认的实现假设。
DRAFT_LEAD_WEIGHTS = {
    j: (5 - ceil(j / 4)) / 40.0 for j in range(1, 17)
}


@dataclass(frozen=True)
class Observation:
    valid_at: datetime
    actual_mw: float | None
    available_mw: float | None = None
    curtailed: bool = False
    approved_exemption: bool = False


@dataclass(frozen=True)
class Forecast:
    issued_at: datetime
    valid_at: datetime
    predicted_mw: float


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("All operational times must be timezone-aware; do not guess a timezone")
    return dt.astimezone(timezone.utc)


def business_day_targets(target_date: date) -> tuple[datetime, ...]:
    """北京业务日的 96 个时段起点；若使用时段终点标签须先显式转换。"""
    if isinstance(target_date, datetime) or not isinstance(target_date, date):
        raise TypeError("target_date must be a date, not a datetime or string")
    begin = datetime.combine(target_date, time.min, tzinfo=BEIJING)
    return tuple(_utc(begin + i * STEP) for i in range(96))


def _check_power(value: float | None, name: str) -> None:
    if value is None:
        return
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative MW")


def _reference(o: Observation, *, use_available_always: bool) -> float | None:
    # 正式版：不限电取实发，限电取可用；草案超短期按原文的可用功率。
    value = o.available_mw if (use_available_always or o.curtailed) else o.actual_mw
    _check_power(value, "reference power")
    return value


def score_day(
    target_date: date,
    product: str,
    capacity_mw: float,
    observations: list[Observation],
    forecasts: list[Forecast],
    *,
    rule_version: str = "GX_WIND_PUBLIC_2025",
) -> dict:
    """按完整 96 点日返回模型评估结果，不把不完整日当作正式成绩。

    day_ahead: 96 点必须来自唯一发布批次。
    ultra_short: 每个目标时刻必须具备前 15 ... 240 分钟的全部 16 次发布。
    不推断有效的调度豁免、不自动修补缺报。保留没有裁零的准确率。
    """
    if rule_version not in POLICIES:
        raise ValueError(f"Unknown policy rule: {rule_version}")
    rule = POLICIES[rule_version]
    if product not in ("day_ahead", "ultra_short"):
        raise ValueError("product must be day_ahead or ultra_short")
    if not isfinite(capacity_mw) or capacity_mw <= 0:
        raise ValueError("capacity_mw must be finite and positive")
    expected = business_day_targets(target_date)
    expected_set = set(expected)
    obs_by_target: dict[datetime, Observation] = {}
    forecasts_by_target: dict[datetime, dict[datetime, float]] = {}
    for o in observations:
        t = _utc(o.valid_at)
        if t not in expected_set:
            continue
        if t in obs_by_target:
            raise ValueError(f"Duplicate observations for {t.isoformat()}; select an as-of revision first")
        _check_power(o.actual_mw, "actual_mw")
        _check_power(o.available_mw, "available_mw")
        obs_by_target[t] = o
    for p in forecasts:
        t = _utc(p.valid_at)
        if t not in expected_set:
            continue
        issued = _utc(p.issued_at)
        if issued >= t:
            raise ValueError("Forecast issue time must precede valid time")
        _check_power(p.predicted_mw, "predicted_mw")
        slots = forecasts_by_target.setdefault(t, {})
        if issued in slots:
            raise ValueError(f"Duplicate forecast ({issued.isoformat()}, {t.isoformat()}); select one series")
        slots[issued] = p.predicted_mw

    if product == "day_ahead":
        issuance_times = {issue for slots in forecasts_by_target.values() for issue in slots}
        if len(issuance_times) > 1:
            raise ValueError("Day-ahead score requires one explicitly selected issuance")
        if issuance_times and next(iter(issuance_times)) >= expected[0]:
            raise ValueError("Day-ahead issuance must precede the target business day")

    target_errors_mw: list[float] = []
    policy_error_terms: list[float] = []
    missing_observations = missing_predictions = missing_version_slots = exemptions = 0
    lead_abs_errors: dict[int, list[float]] = {15*j: [] for j in range(1, 17)}
    for t in expected:
        o = obs_by_target.get(t)
        if o is None:
            missing_observations += 1
            continue
        if o.approved_exemption:
            exemptions += 1
            continue
        reference = _reference(
            o, use_available_always=(product == "ultra_short" and
                                      rule["ultra_short_reference"] == "available"),
        )
        if reference is None:
            missing_observations += 1
            continue
        denom = max(reference, 0.2 * capacity_mw)
        slots = forecasts_by_target.get(t, {})
        if product == "day_ahead":
            if not slots:
                missing_predictions += 1
                continue
            pred = next(iter(slots.values()))
            error = pred - reference
            target_errors_mw.append(error)
            policy_error_terms.append((error / denom) ** 2)
            continue

        required = [(j, t - j*STEP) for j in range(1, 17)]
        for j, issued in required:
            if issued in slots:
                lead_abs_errors[15*j].append(abs(slots[issued] - reference))
        absent = [j for j, issued in required if issued not in slots]
        if absent:
            missing_predictions += 1
            missing_version_slots += len(absent)
            continue
        pred = mean(slots[issued] for _, issued in required)
        error = pred - reference
        target_errors_mw.append(error)
        if rule["ultra_short_mode"] == "mean_then_daily_rmse":
            policy_error_terms.append((error / denom) ** 2)
        else:
            policy_error_terms.append(sqrt(sum(
                DRAFT_LEAD_WEIGHTS[j] * ((slots[issued] - reference) / denom) ** 2
                for j, issued in required
            )))

    complete = (len(target_errors_mw) == 96 and not exemptions and
                not missing_predictions and not missing_observations)
    reasons = []
    if missing_observations:
        reasons.append("missing_or_unusable_reference")
    if missing_predictions:
        reasons.append("missing_prediction_or_issuance")
    if exemptions:
        reasons.append("exemption_denominator_requires_dispatch_confirmation")
    accuracy = None
    if complete:
        if product == "ultra_short" and rule["ultra_short_mode"] != "mean_then_daily_rmse":
            accuracy = 1 - mean(policy_error_terms)
        else:
            accuracy = 1 - sqrt(mean(policy_error_terms))
    threshold = rule["thresholds"][product]
    n = len(target_errors_mw)
    result = {
        "target_date": target_date.isoformat(),
        "product": product,
        "rule_version": rule_version,
        "rule_status": rule["status"],
        "evaluation_scope": "research_replay_not_dispatch_certified",
        "status": "complete" if complete else "incomplete",
        "incomplete_reasons": reasons,
        "required_target_count": 96,
        "scored_target_count": n,
        "missing_reference_targets": missing_observations,
        "missing_prediction_targets": missing_predictions,
        "missing_ultra_short_version_slots": missing_version_slots,
        "approved_exemption_targets": exemptions,
        "capacity_mw": capacity_mw,
        "MAE_mw": mean(abs(e) for e in target_errors_mw) if complete else None,
        "RMSE_mw": sqrt(mean(e*e for e in target_errors_mw)) if complete else None,
        "GX_policy_accuracy": accuracy,
        "GX_policy_threshold": threshold,
        "GX_policy_pass": (accuracy >= threshold) if complete else None,
    }
    if product == "ultra_short":
        result["lead_MAE_mw_diagnostic"] = {
            str(lead): mean(errors) if errors else None
            for lead, errors in lead_abs_errors.items()
        }
        result["draft_weight_assumption"] = (
            "hourly_4_3_2_1_equal_within_hour" if rule_version == "GX_WIND_DRAFT_202608"
            else None
        )
    return result
