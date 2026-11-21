"""as-of 样本构造测试：防泄漏、时间边界、预测矩阵与既有协议保护（T04）。"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from tests.synthetic_scada import T0, STEP, frame, record, step_of, tiny_config
from windpower.features import scada_samples
from windpower.features.scada_samples import (
    FEATURE_COLUMNS, build_samples, split_by_target,
)


@pytest.fixture()
def config(tmp_path):
    return tiny_config(tmp_path / 'synth_config.json')


def _build(cfg_path, steps=700, **kw):
    f = frame(steps, **kw)
    return build_samples(f, config_path=cfg_path)


def test_lag_features_use_only_records_before_issue(config):
    """power 编码时间步序号：任何未来信息进入特征都会表现为数值错位。"""
    samples, stats = _build(config)
    issue_steps = samples['issue_at'].map_elements(step_of, return_dtype=pl.Int64)
    # p_lag1 必须等于 T-10 分钟（最后一个完整可见区间）的步序号
    assert (samples['p_lag1'] == issue_steps - 1).all()
    assert (samples['p_lag2'] == issue_steps - 2).all()
    assert (samples['p_lag144'] == issue_steps - 144).all()
    assert (samples['p_chg_1_2'] == samples['p_lag1'] - samples['p_lag2']).all()
    # 窗口统计同样只来自可见窗口（步序号 s-1..s-144 的均值为 s-72.5）
    assert (samples['p_mean144'] == issue_steps - 72.5).all()
    assert stats['issues_valid'] == 553 and stats['coverage_share'] == 1.0


def test_target_alignment_and_leads(config):
    samples, _ = _build(config)
    assert samples['lead_minutes'].unique().sort().to_list() == [10, 20, 30]
    delta = (samples['valid_at'] - samples['issue_at']).dt.total_minutes()
    assert (delta == samples['lead_minutes']).all()
    # 同一目标时刻可对应多个发布时刻：目标 t 出现在 t-10、t-20、t-30 的发布中
    one = samples.filter(pl.col('valid_at') == T0 + timedelta(hours=48))
    assert one['issue_at'].n_unique() == 3
    assert sorted(one['lead_minutes'].to_list()) == [10, 20, 30]
    # 标签来自目标时刻记录
    assert (one['actual_kw'] == step_of(T0 + timedelta(hours=48)) % 5000).all()


def test_seasonal_reference_uses_same_target_time_previous_days(config):
    samples, _ = _build(config)
    t = T0 + timedelta(hours=96)
    row = samples.filter((pl.col('valid_at') == t) & (pl.col('lead_minutes') == 10)).to_dicts()[0]
    refs = [step_of(t - timedelta(days=d)) % 5000 for d in (1, 2)]
    assert row['seasonal_kw'] == sum(refs) / 2
    assert row['seasonal_days_used'] == 2


def test_visibility_delay_blocks_issue_time_record(config):
    """发布时刻 T 的记录（统计时间=T）不可见：滞后一格才可用。"""
    samples, _ = _build(config)
    issue_steps = samples['issue_at'].map_elements(step_of, return_dtype=pl.Int64)
    # 若把 T 当可见，p_lag1 会等于 issue 步序号本身；必须恒为 -1。
    assert not (samples['p_lag1'] == issue_steps).any()


def test_incomplete_history_drops_only_affected_issues(config):
    samples, stats = _build(config, skip=(200,))
    # 缺失记录 step=200 杀死窗口含 200 的发布：issue 201..344（144 步窗口）
    issue_steps = {step_of(t) for t in samples['issue_at'].unique().to_list()}
    assert all(s not in issue_steps for s in range(201, 345))
    assert 200 in issue_steps and 345 in issue_steps
    assert stats['dropped_incomplete_history'] == 144


def test_masked_pitch_drops_issue(config):
    f = frame(700).with_columns(
        pl.when(pl.col('observed_at') == T0 + STEP * 300)
        .then(pl.lit(-182.41)).otherwise(pl.col('pitch1_deg')).alias('pitch1_deg'))
    samples, stats = build_samples(f, config_path=config)
    issue_steps = {step_of(t) for t in samples['issue_at'].unique().to_list()}
    # 桨距只进入 lag1：只有发布 step 301 的 lag1 指向 300，被掩码剔除
    assert stats['dropped_missing_feature'] == 1
    assert 301 not in issue_steps and 302 in issue_steps


def test_splits_are_left_closed_right_open_by_target(config):
    samples, _ = _build(config)
    parts = {n: split_by_target(samples, n, config_path=config)
             for n in ('train', 'validation', 'test')}
    start = datetime(2024, 1, 3)
    assert parts['train']['valid_at'].max() < start
    assert parts['validation']['valid_at'].min() == start
    # 同一 (发布, 目标) 样本绝不在两个分段出现
    keys = [set(p.select('issue_at', 'valid_at', 'lead_minutes').rows()) for p in parts.values()]
    assert not (keys[0] & keys[1]) and not (keys[1] & keys[2])
    # 同一发布可跨段（不同目标归不同段），目标标签唯一归属
    boundary_issue = T0 + timedelta(days=1, hours=23, minutes=40)  # step 286
    train_targets = parts['train'].filter(pl.col('issue_at') == boundary_issue)['valid_at']
    val_targets = parts['validation'].filter(pl.col('issue_at') == boundary_issue)['valid_at']
    assert train_targets.max() < start and val_targets.min() >= start
    assert set(train_targets.dt.minute().to_list()) == {50}      # 23:50 归训练
    assert val_targets.min() == start                             # 00:00 起归验证


def test_feature_columns_exclude_labels_and_diagnostics():
    """结构级防泄漏：输入列不得包含标签或评估诊断列。"""
    text = '\n'.join(FEATURE_COLUMNS)
    assert 'actual' not in text and 'diag_' not in text and 'seasonal_kw' not in text
    assert 'valid_at' not in text and 'issue_at' not in text


def test_gefcom_protocol_unchanged():
    """F01 独立划分不影响 GEFCom2014 的 2012/2013 协议。"""
    cfg = json.loads(Path('configs/experiment.json').read_text())
    assert cfg['splits']['train'] == ['2012-01-01', '2013-01-01']
    assert cfg['splits']['validation'] == ['2013-01-01', '2013-07-01']
    assert cfg['splits']['test'] == ['2013-07-01', '2014-01-01']


def test_time_semantics_not_silently_converted(config):
    """源 naive 时间不得被转换为带时区时间。"""
    samples, _ = _build(config)
    assert samples['issue_at'].dtype == pl.Datetime('us')
    assert samples.schema['valid_at'] == pl.Datetime('us')
