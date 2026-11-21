"""质量规则单测：哨兵值、全缺失、零方差、负功率保留、时间缺口（T03）。"""
from datetime import datetime, timedelta

from windpower.data.quality_scada import (
    ALL_MISSING_COLUMNS, parse_number, row_flags, column_profile, rule_documentation,
    time_axis_report,
)

T = datetime(2024, 1, 1)


def test_parse_number_dash_empty_and_values():
    assert parse_number('--') == (None, 'dash')
    assert parse_number(' -- ') == (None, 'dash')
    assert parse_number('') == (None, 'empty')
    assert parse_number(None) == (None, 'empty')
    assert parse_number('847') == (847.0, None)
    assert parse_number('-31.92') == (-31.92, None)
    assert parse_number('abc') == (None, 'unparseable')


def _values(**over):
    v = dict(grid_active_power_kw=100.0, pitch1_deg=0.0, pitch2_deg=5.0,
             pitch3_deg=10.0, yaw_position_deg=15.0, ambient_temp_c=20.0,
             nacelle_wind_speed_ms=6.0, generating_h=1 / 6, fault_h=0.0,
             maintenance_h=0.0, normal_h=1 / 6, env_abnormal_h=0.0, env_normal_h=1 / 6,
             **{'_raw_平均偏航集电环温度(℃)': 40.0,
                '_raw_平均发电机进风口温度1(℃)': 847.0,
                '_raw_平均发电机进风口温度2(℃)': 0.0})
    v.update(over)
    return v


def test_negative_power_flagged_not_removed():
    flags = row_flags(_values(grid_active_power_kw=-31.92), None, T)
    assert flags['negative_grid_power'] == -31.92  # 保留原值
    assert 'zero_grid_power' not in flags


def test_zero_power_is_flag_informationally():
    flags = row_flags(_values(grid_active_power_kw=0.0), None, T)
    assert flags['zero_grid_power'] == 0


def test_sentinel_847_intake_temp_flagged():
    flags = row_flags(_values(), None, T)
    assert flags['sentinel_847_intake_temp1'] == 847.0
    assert 'sentinel_847_intake_temp2' not in flags


def test_yaw_ring_temp_anomaly_threshold():
    assert 'yaw_ring_temp_anomaly' not in row_flags(_values(), None, T)
    flags = row_flags(_values(**{'_raw_平均偏航集电环温度(℃)': 847.03}), None, T)
    assert flags['yaw_ring_temp_anomaly'] == 847.03


def test_pitch_outside_canonical_keeps_worst_value():
    flags = row_flags(_values(pitch2_deg=-128.1), None, T)
    assert 'pitch_outside_canonical' in flags
    assert flags['pitch_outside_canonical'] == -128.1


def test_yaw_position_outside_canonical_and_ambient_wind_masks():
    flags = row_flags(_values(yaw_position_deg=-681.94, ambient_temp_c=99.0,
                              nacelle_wind_speed_ms=-1.0), None, T)
    assert flags['yaw_position_outside_canonical'] == -681.94
    assert flags['ambient_temp_outside_physical'] == 99.0
    assert flags['wind_speed_outside_physical'] == -1.0


def test_state_duration_exceeds_step_is_informational():
    flags = row_flags(_values(normal_h=3.0), None, T)
    assert flags['state_duration_exceeds_step'] == 3.0


def test_after_time_gap_only_when_delta_differs():
    no_gap = row_flags(_values(), T - timedelta(minutes=10), T)
    assert 'after_time_gap' not in no_gap
    gap = row_flags(_values(), T - timedelta(minutes=180), T)
    assert gap['after_time_gap'] == 18


def test_time_axis_report_counts_gaps_and_missing_grid():
    times = [T + timedelta(minutes=10 * i) for i in range(10)]
    times += [T + timedelta(minutes=210 + 10 * i) for i in range(5)]  # 120 分钟缺口
    report = time_axis_report(times)
    assert report['rows'] == 15
    assert report['expected_grid_points'] == 26
    assert report['missing_grid_points'] == 11
    assert report['duplicate_timestamps'] == 0
    assert report['monotonic'] is True
    assert report['gap_events'] == 1
    assert report['max_gap_minutes'] == 120.0
    assert report['largest_gaps'][0]['delta_minutes'] == 120.0


def test_column_profile_classifies_missing_constant_sentinel():
    assert column_profile('有效风时数<=3(h)', ['--'] * 5)['all_missing'] is True
    profile = column_profile('平均X轴方向加速度', ['0', '0', '0'])
    assert profile['zero_variance'] is True and profile['unique_count'] == 1
    sentinel = column_profile('平均发电机进风口温度1(℃)', ['847', '847', '0'])
    assert sentinel['sentinel_847_count'] == 2
    assert sentinel['first_round_usage'] == 'excluded_sentinel_only_channel'
    assert column_profile('有效风时数>19(h)', ['--'] * 3)['first_round_usage'] == 'excluded_all_missing'
    assert column_profile('平均网侧有功功率(kW)', ['1.5'])['first_round_usage'] == 'typed_feature_or_label'


def test_rule_documentation_covers_units_and_handling():
    rules = rule_documentation()
    assert rules['negative_grid_power']['unit'] == 'kW'
    assert '不得截零' in rules['negative_grid_power']['handling']
    assert len(ALL_MISSING_COLUMNS) == 4
