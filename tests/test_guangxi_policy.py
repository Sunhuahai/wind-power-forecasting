"""广西政策公式的合成单测：不打开 GEFCom2014 测试集，不连接数据库。"""
from datetime import date, datetime, timedelta, timezone
from math import isclose

import pytest

from windpower.evaluation.guangxi import (
    BEIJING, STEP, DRAFT_LEAD_WEIGHTS, Forecast, Observation,
    business_day_targets, score_day,
)

DAY = date(2026, 11, 22)
ISSUED = datetime(2026, 11, 21, 8, 45, tzinfo=BEIJING)


def obs(actual=20.0, *, available=None, curtailed=False, exempt=False):
    return [
        Observation(t, actual, available, curtailed, exempt)
        for t in business_day_targets(DAY)
    ]


def da(predicted=28.0):
    return [Forecast(ISSUED, t, predicted) for t in business_day_targets(DAY)]


def ultra(value):
    """value(j, target_index) 返回提前 j*15min 的预测功率。"""
    return [
        Forecast(t - j * STEP, t, float(value(j, i)))
        for i, t in enumerate(business_day_targets(DAY))
        for j in range(1, 17)
    ]


def test_beijing_day_has_96_start_labels_and_midnight_history():
    times = business_day_targets(DAY)
    assert len(times) == len(set(times)) == 96
    assert times[0].astimezone(BEIJING).strftime("%H:%M") == "00:00"
    assert times[-1].astimezone(BEIJING).strftime("%H:%M") == "23:45"
    assert (times[0] - 16 * STEP).astimezone(BEIJING).day == 21


def test_day_ahead_capacity_floor_not_plain_nrmse():
    result = score_day(DAY, "day_ahead", 100, obs(), da())
    assert result["status"] == "complete"
    assert result["scored_target_count"] == 96
    assert isclose(result["MAE_mw"], 8.0)
    assert isclose(result["RMSE_mw"], 8.0)
    assert isclose(result["GX_policy_accuracy"], 0.60)
    assert result["GX_policy_pass"] is True


def test_official_short_term_mean_cancels_but_draft_does_not():
    predictions = ultra(lambda j, _: 40 if j % 2 else 60)
    actual = obs(50, available=50)
    public = score_day(DAY, "ultra_short", 100, actual, predictions)
    draft = score_day(DAY, "ultra_short", 100, actual, predictions,
                      rule_version="GX_WIND_DRAFT_202608")
    assert isclose(public["GX_policy_accuracy"], 1.0)
    assert isclose(draft["GX_policy_accuracy"], 0.80)
    assert isclose(sum(DRAFT_LEAD_WEIGHTS.values()), 1.0)
    assert isclose(DRAFT_LEAD_WEIGHTS[1], 0.1)
    assert isclose(DRAFT_LEAD_WEIGHTS[16], 0.025)


def test_draft_root_is_inside_each_target_not_outside_day():
    predicted = ultra(lambda j, i: 50 if i < 48 else 70)
    actual = obs(50, available=50)
    public = score_day(DAY, "ultra_short", 100, actual, predicted)
    draft = score_day(DAY, "ultra_short", 100, actual, predicted,
                      rule_version="GX_WIND_DRAFT_202608")
    assert isclose(public["GX_policy_accuracy"], 1 - (0.4**2 / 2)**0.5)
    assert isclose(draft["GX_policy_accuracy"], 0.8)


def test_reference_uses_available_power_when_curtailed():
    r = score_day(DAY, "day_ahead", 100,
                  obs(30, available=70, curtailed=True), da(70))
    assert r["GX_policy_accuracy"] == 1.0


def test_draft_ultra_short_requires_available_power():
    r = score_day(DAY, "ultra_short", 100, obs(50),
                  ultra(lambda j, i: 50),
                  rule_version="GX_WIND_DRAFT_202608")
    assert r["GX_policy_accuracy"] is None
    assert r["missing_reference_targets"] == 96


def test_one_missing_historical_issue_prevents_full_score():
    p = ultra(lambda j, i: 20)
    p.pop(0)
    r = score_day(DAY, "ultra_short", 100, obs(), p)
    assert r["status"] == "incomplete"
    assert r["GX_policy_accuracy"] is None
    assert r["missing_ultra_short_version_slots"] == 1


def test_approved_exemption_requires_known_denominator():
    r = score_day(DAY, "day_ahead", 100,
                  obs(20, exempt=True), da())
    assert r["status"] == "incomplete"
    assert r["approved_exemption_targets"] == 96
    assert "exemption_denominator_requires_dispatch_confirmation" in r["incomplete_reasons"]


def test_day_ahead_refuses_mixed_issuances():
    p = da()
    p[0] = Forecast(ISSUED - STEP, p[0].valid_at, 28)
    with pytest.raises(ValueError, match="one explicitly selected issuance"):
        score_day(DAY, "day_ahead", 100, obs(), p)


def test_naive_timestamps_are_not_silently_accepted():
    p = da()
    p[0] = Forecast(datetime(2026, 11, 21, 8, 45),
                    p[0].valid_at, 28)
    with pytest.raises(ValueError, match="timezone-aware"):
        score_day(DAY, "day_ahead", 100, obs(), p)


def test_duplicate_forecast_version_fails():
    p = da()
    with pytest.raises(ValueError, match="Duplicate forecast"):
        score_day(DAY, "day_ahead", 100, obs(), p + [p[0]])


def test_large_errors_are_not_clipped_to_zero():
    r = score_day(DAY, "day_ahead", 100, obs(20), da(60))
    assert isclose(r["GX_policy_accuracy"], -1.0)


def test_missing_observation_not_interpolated():
    r = score_day(DAY, "day_ahead", 100, obs()[:-1], da())
    assert r["status"] == "incomplete"
    assert r["missing_reference_targets"] == 1


def test_zero_power_uses_capacity_floor():
    r = score_day(DAY, "day_ahead", 100, obs(0), da(0))
    assert isclose(r["GX_policy_accuracy"], 1.0)
