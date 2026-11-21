"""单机评价测试：基础指标算例、分组、门禁与政策资格控制。"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from windpower.evaluation import scada_eval


def _frame(rows):
    return pl.DataFrame(rows)


def test_basic_metrics_known_example():
    m = scada_eval.basic_metrics([0.0, 10.0], [5.0, 5.0])
    assert m['MAE'] == pytest.approx(5.0)
    assert m['RMSE'] == pytest.approx(5.0)
    assert m['Bias'] == pytest.approx(0.0)
    assert m['n'] == 2


def test_basic_metrics_asymmetric_error():
    m = scada_eval.basic_metrics([10.0, 10.0, 10.0], [25.0, 10.0, -5.0])
    assert m['MAE'] == pytest.approx(10.0)
    assert m['RMSE'] == pytest.approx(12.2474487, rel=1e-6)
    assert m['Bias'] == pytest.approx(0.0)


def _scored(model='m'):
    """3 个发布 × 2 个提前量，功率含负值（不得被裁剪）。"""
    base = datetime(2024, 1, 1)
    rows = []
    for i, issue in enumerate([0, 10, 20]):
        for lead in (10, 20):
            rows.append(dict(
                model=model, issue_at=base + timedelta(minutes=issue),
                valid_at=base + timedelta(minutes=issue + lead),
                lead_minutes=lead,
                actual_kw=[-20.0, 500.0, 4800.0][i],
                prediction_kw=[-15.0, 400.0, 4600.0][i],
                p_lag1=[-25.0, 450.0, 4900.0][i],
                diag_wind_ms=[1.0, 7.0, 13.0][i],
                diag_gen_h=[0.0, 1 / 6, 1 / 6][i],
                diag_fault_h=[0.0, 0.0, 0.0][i],
                diag_maint_h=[0.0, 0.0, 1 / 6][i],
                diag_env_abn_h=[0.0, 0.0, 0.0][i],
                diag_dp30_kw=[0.0, 2000.0, -3000.0][i]))
    return _frame(rows)


def test_per_lead_and_per_day_aggregation():
    f = _scored()
    leads = scada_eval.per_lead(f)
    assert {r['lead_minutes'] for r in leads} == {10, 20}
    days = scada_eval.per_day(f)
    assert len(days) == 1 and days[0]['n'] == 6 and days[0]['n_issues'] == 3


def test_wind_power_state_group_reports():
    f = _scored()
    wind = scada_eval.wind_groups(f, [3, 6, 9, 12])
    groups = {g['group']: g for g in wind}
    assert groups['wind_ms_~3']['n'] == 2      # 1 m/s 组
    assert groups['wind_ms_6~9']['n'] == 2
    assert groups['wind_ms_12~']['n'] == 2
    power = scada_eval.power_groups(f, train_max_kw=5000.0,
                                    low_fraction=0.05, high_fraction=0.9)
    names = {g['group'] for g in power}
    assert 'power_negative' in names and 'power_high' in names
    state = {g['group']: g for g in scada_eval.state_groups(f)}
    assert state['state_not_generating']['n'] == 2
    assert state['state_generating']['n'] == 4


def test_ramp_report_threshold_and_direction():
    f = _scored()
    ramp = scada_eval.ramp_report(f, threshold_kw=1000.0)
    assert ramp['events'] == 4                 # |2000|、|-3000| 各出现在两个提前量
    assert ramp['direction_accuracy'] == pytest.approx(0.5)


def test_negative_predictions_are_not_clipped():
    f = _scored()
    m = scada_eval.basic_metrics(f['actual_kw'], f['prediction_kw'])
    assert m['MAE'] == pytest.approx(305.0 / 3.0)   # (5+100+200)*2/6，负值不裁剪


def test_gate_check_criteria():
    cfg = {'gate': {'name': 'G', 'action_on_pass': 'x'}}
    results = {
        'persistence': {'RMSE': 1000.0, 'per_lead': [{'RMSE': 900.0}, {'RMSE': 1100.0}]},
        'lightgbm': {'RMSE': 900.0, 'per_lead': [{'RMSE': 800.0}, {'RMSE': 1000.0}]},
    }
    gate = scada_eval.gate_check(results, tests_passed=True, cfg=cfg)
    assert gate['status'] == 'pass'
    assert gate['criteria']['c1_pooled_rmse_beats_persistence'] is True
    worse = dict(results, lightgbm={'RMSE': 1200.0,
                                    'per_lead': [{'RMSE': 1200.0}, {'RMSE': 1300.0}]})
    gate2 = scada_eval.gate_check(worse, tests_passed=True, cfg=cfg)
    assert gate2['status'] == 'fail'


def test_single_turbine_policy_fields_are_null_not_zero():
    cfg = json.loads(Path('configs/f01_experiment.json').read_text())
    policy = scada_eval.policy_block(cfg)
    assert policy['GX_policy_accuracy'] is None      # null，不是 0
    assert policy['eligible'] is False
    assert policy['ineligible_reason'] == (
        'single_turbine_10m_scada_is_not_farm_dispatch_reference')
    assert 'GX_WIND_PUBLIC_2025' in policy['rule_versions']


def test_write_reports_produces_unified_outputs(tmp_path):
    cfg = json.loads(Path('configs/f01_experiment.json').read_text())
    f = _scored('persistence').drop('model')
    preds = {'persistence': f['prediction_kw'].to_numpy(),
             'lightgbm': (f['actual_kw'].to_numpy() + 1.0)}
    runtimes = {m: dict(fit_seconds=0.01, predict_seconds=0.01) for m in preds}
    results = {'persistence': dict(
                    model='persistence', **scada_eval.pooled(f),
                    per_lead=scada_eval.per_lead(f), per_day=scada_eval.per_day(f),
                    wind_groups=[], state_groups=[], power_groups=[], ramp={},
                    runtime={}),
                'lightgbm': dict(
                    model='lightgbm',
                    **scada_eval.basic_metrics(f['actual_kw'], f['actual_kw'] + 1.0),
                    per_lead=[], per_day=[], wind_groups=[], state_groups=[],
                    power_groups=[], ramp={}, runtime={})}
    thresholds = dict(train_max_kw=5000.0, ramp_threshold_kw=1000.0,
                      ramp_quantile=0.95, low_cut_fraction=0.05, high_cut_fraction=0.9)
    summary = scada_eval.write_reports(
        tmp_path, 'validation', results, thresholds,
        coverage=dict(issues_valid=3, coverage_share=1.0,
                      dropped_incomplete_history=0, dropped_missing_feature=0,
                      dropped_incomplete_labels=0),
        cfg=cfg, test_availability=dict(status='locked', rows=0, issues=0, note=''),
        tests_passed=True)
    for name in ('summary.json', 'per_lead.csv', 'per_day.csv', 'report.md'):
        assert (tmp_path / name).exists(), name
    saved = json.loads((tmp_path / 'summary.json').read_text())
    assert saved['policy']['GX_policy_accuracy'] is None
    assert saved['test_set']['status'] == 'locked'
    assert saved['gate']['criteria']['c1_pooled_rmse_beats_persistence'] is True


def test_prediction_length_mismatch_is_rejected():
    f = _scored('persistence')
    with pytest.raises(ValueError, match='prediction length'):
        scada_eval.evaluate_split(f, preds={'persistence': [1.0, 2.0]},
                                  runtimes={}, thresholds={},
                                  cfg={'evaluation': {'wind_bins_ms': [3],
                                                      'power_group_factors': {
                                                          'low_max_fraction': 0.05,
                                                          'high_fraction': 0.9},
                                                      'ramp': {}}})
