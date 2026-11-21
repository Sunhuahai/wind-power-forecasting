"""10->15 分钟代理换算测试（T05 前置）：权重、缺段不重归一化、research_only。"""
from datetime import datetime, timedelta

import pytest

from windpower.data.resample_15m import resample_10m_to_15m

T = datetime(2024, 1, 1)


def _records(values):
    return [(T + timedelta(minutes=10 * i), v, i + 1) for i, v in enumerate(values)]


def test_doc_example_overlap_weights():
    """规划 §6 算例：1000/1500/2000 -> 1166.67 / 1833.33 kW。"""
    out = resample_10m_to_15m(_records([1000.0, 1500.0, 2000.0]), T, 2)
    assert [r['valid_at'] for r in out] == [T, T + timedelta(minutes=15)]
    assert out[0]['proxy_kw'] == pytest.approx((10 * 1000 + 5 * 1500) / 15)
    assert out[0]['proxy_kw'] == pytest.approx(1166.67, abs=0.01)
    assert out[1]['proxy_kw'] == pytest.approx((5 * 1500 + 10 * 2000) / 15)
    assert out[1]['proxy_kw'] == pytest.approx(1833.33, abs=0.01)
    assert out[0]['covered_minutes'] == 15
    assert out[0]['contributing_rows'] == [
        {'row_ref': 1, 'observed_at': str(T), 'minutes': 10},
        {'row_ref': 2, 'observed_at': str(T + timedelta(minutes=10)), 'minutes': 5}]


def test_missing_contributing_interval_means_absent_slot():
    records = _records([100.0, None, 300.0])
    out = resample_10m_to_15m(records, T, 2)
    # 第一时段 [00:00,00:15) 需要 00:00 与 00:10；00:10 缺失 -> 时段缺席
    # 第二时段需要 00:10 与 00:20；00:10 缺失 -> 也缺席
    assert out == []
    # 不按剩余样本重新归一化：缺一段即无输出
    full = resample_10m_to_15m(_records([100.0, 200.0, 300.0]), T, 2)
    assert len(full) == 2 and full[0]['covered_minutes'] == 15


def test_research_only_and_unconfirmed_semantics():
    out = resample_10m_to_15m(_records([10.0, 20.0, 30.0]), T, 1)
    assert out[0]['research_only'] is True
    assert out[0]['label_semantics_confirmed'] is False
    assert out[0]['method'] == 'overlap_weighted_from_10m_interval_start'


def test_interval_end_semantics_refused_until_confirmed():
    with pytest.raises(ValueError, match='source system'):
        resample_10m_to_15m(_records([10.0, 20.0]), T, 1, assume_interval_start=False)


def test_off_grid_and_duplicate_inputs_rejected():
    with pytest.raises(ValueError, match='quarter-hour'):
        resample_10m_to_15m(_records([10.0]), T + timedelta(minutes=7), 1)
    dup = _records([10.0, 20.0]) + [(T, 99.0, 3)]
    with pytest.raises(ValueError, match='Duplicate'):
        resample_10m_to_15m(dup, T, 1)


def test_partial_tail_window_dropped_not_renormalized():
    records = _records([10.0, 20.0, 30.0])
    # 只给到 00:20；第二时段 [00:15,00:30) 需要 00:15 处不存在的起点 -> 缺席
    out = resample_10m_to_15m(records, T, 3)
    assert [r['valid_at'].minute for r in out] == [0, 15]
    assert out[0]['proxy_kw'] == pytest.approx((10 * 10 + 5 * 20) / 15)
    assert out[1]['proxy_kw'] == pytest.approx((5 * 20 + 10 * 30) / 15)
