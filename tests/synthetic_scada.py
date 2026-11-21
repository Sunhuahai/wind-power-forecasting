"""测试共用的合成 SCADA 构造器：确定性格局，供防泄漏与管道测试使用。

功率取值编码其时间步序号（power = step index），使任何"未来信息进入特征"
都表现为可检测的数值错位。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

T0 = datetime(2024, 1, 1)
STEP = timedelta(minutes=10)

FIELD_COLUMNS = {
    '平均网侧有功功率(kW)': 'grid_active_power_kw',
    '平均变流器有功功率(kW)': 'converter_active_power_kw',
    '平均变流器无功功率(kVar)': 'converter_reactive_kvar',
    '平均网侧无功功率(kVar)': 'grid_reactive_kvar',
    '平均风速(m/s)': 'nacelle_wind_speed_ms',
    '平均环境温度(℃)': 'ambient_temp_c',
    '平均叶轮转速瞬时值(rpm)': 'rotor_rpm',
    '平均发电机转速瞬时值(rpm)': 'generator_rpm',
    '平均1#变桨桨距角(deg)': 'pitch1_deg',
    '平均2#变桨桨距角(deg)': 'pitch2_deg',
    '平均3#变桨桨距角(deg)': 'pitch3_deg',
    '平均偏航位置(deg)': 'yaw_position_deg',
    '平均对风角度(deg)': 'yaw_error_deg',
    '风机正常运行时间(h)': 'normal_h',
    '风机发电时间(h)': 'generating_h',
    '风机故障时间(h)': 'fault_h',
    '风机维护时间(h)': 'maintenance_h',
    '风机环境异常时间(h)': 'env_abnormal_h',
    '风机环境正常时间(h)': 'env_normal_h',
}
IDENTITY_COLUMNS = ['序号', '馈线名称', '设备名称', '型号名称', '统计时间']
MONITORED = ['平均偏航集电环温度(℃)', '平均发电机进风口温度1(℃)', '平均发电机进风口温度2(℃)']


def step_of(t: datetime) -> int:
    return int((t - T0).total_seconds() // 600)


def record(step: int, **overrides) -> dict:
    """step 序号唯一决定全部数值；任何错位都可被逐位核对。"""
    t = T0 + step * STEP
    row = dict(
        observed_at=t,
        grid_active_power_kw=float(step % 5000),
        converter_active_power_kw=float(step % 5000) - 2.0,
        converter_reactive_kvar=1.0, grid_reactive_kvar=2.0,
        nacelle_wind_speed_ms=5.0 + step % 7,
        ambient_temp_c=15.0 + (step % 20) * 0.1,
        rotor_rpm=float(step % 9), generator_rpm=100.0 + step % 50,
        pitch1_deg=0.0, pitch2_deg=0.0, pitch3_deg=0.0,
        yaw_position_deg=10.0 + step, yaw_error_deg=float(step % 30 - 15),
        normal_h=1 / 6, generating_h=1 / 6 if step % 5000 > 100 else 0.0,
        fault_h=0.0, maintenance_h=0.0,
        env_abnormal_h=0.0, env_normal_h=1 / 6,
        file_id='synthetic-test',
    )
    row.update(overrides)
    return row


def frame(steps: int, skip: tuple[int, ...] = ()) -> pl.DataFrame:
    rows = [record(s) for s in range(steps) if s not in skip]
    return pl.DataFrame(rows).sort('observed_at')


def tiny_config(path: Path) -> Path:
    cfg = {
        'task': 'SYNTH_TEST_10M',
        'availability_assumption': {
            'name': 'OFFLINE_REPLAY_COMPLETED_INTERVAL',
            'visibility_delay_minutes': 10,
            'statement': 'test assumption'},
        'splits': {
            'train': ['2024-01-01 00:00', '2024-01-03 00:00'],
            'validation': ['2024-01-03 00:00', '2024-01-05 00:00'],
            'test': ['2024-01-05 00:00', '2024-01-07 00:00'],
        },
        'leads_steps': 3,
        'history': {
            'required_complete_steps': 144,
            'feature_mask_rules': {
                'pitch_deg': [-10, 100], 'ambient_temp_c': [-40, 50],
                'nacelle_wind_speed_ms': [0, 60]}},
        'models': {
            'seasonal': {'days': 2, 'rule': 'test'},
            'ridge': {'alphas': [1.0]},
            'lightgbm': {'params': {'n_estimators': 8, 'learning_rate': 0.3,
                                    'num_leaves': 7, 'min_child_samples': 5,
                                    'random_state': 0, 'n_jobs': 2, 'verbosity': -1},
                         'early_stopping_rounds': 4},
        },
        'evaluation': {
            'wind_bins_ms': [3, 6], 'state_groups': ['generating'],
            'power_group_factors': {'low_max_fraction': 0.05, 'high_fraction': 0.9},
            'ramp': {'window_minutes': 30, 'train_abs_change_quantile': 0.95}},
        'policy_eligibility': {
            'eligible': False, 'GX_policy_accuracy': None,
            'ineligible_reason': 'single_turbine_10m_scada_is_not_farm_dispatch_reference',
            'rule_versions': []},
        'gate': {'name': 'TEST_GATE', 'action_on_pass': 'keep test locked'},
    }
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1))
    return path


def synth_csv(path: Path, rows: list[dict], skip_steps: tuple[int, ...] = ()) -> Path:
    """写 GB18030 编码、含 '--' 与哨兵值的合成 CSV（read_raw/audit/ingest 用）。"""
    header = (IDENTITY_COLUMNS + list(FIELD_COLUMNS) + MONITORED)
    lines = [','.join(header)]
    n = 0
    for s in range(len(rows)):
        if s in skip_steps:
            continue
        r = rows[s]
        n += 1
        cells = [str(n), '测试馈线', 'T99', 'TEST/100', r['observed_at'].strftime('%Y/%m/%d %H:%M')]
        for src, dst in FIELD_COLUMNS.items():
            cells.append(_cell(r[dst]))
        cells += ['--', '847', '0']  # 集电环温度缺失；进风口1 哨兵；进风口2 正常
        lines.append(','.join(cells))
    path.write_bytes('\n'.join(lines).encode('gb18030'))
    return path


def _cell(v) -> str:
    if v is None:
        return '--'
    return str(v)
