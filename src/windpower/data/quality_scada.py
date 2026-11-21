"""F01 SCADA 数据质量规则：预注册、可审计，只标记不删改。

所有阈值为先验物理/工程常识或对已知哨兵值的显式命名，均未使用验证/测试数据选择。
规则输出三类信息：
- 行级 quality_flags：随规范行入库，保留原值（负功率、哨兵值不截断、不覆盖）。
- 列级 profile：全缺失、零方差、哨兵占位通道等，第一轮特征排除原因。
- 文件级 summary：时间网格、缺口、规则命中计数，进 quality_report.json。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

STEP = timedelta(minutes=10)

# 已核实的哨兵/占位值：进风口温度1/2 只有 0 与 847 两种取值，847 非物理温度。
SENTINEL_TEMP_847 = 847.0
SENTINEL_TEMP_COLUMNS = ('平均发电机进风口温度1(℃)', '平均发电机进风口温度2(℃)')

# 预注册的行级规则阈值（a priori，非按结果调参）。
RULES = {
    'yaw_ring_temp_above_c': 200.0,          # 机舱环温超过 200℃ 非物理
    'pitch_canonical_deg': (-10.0, 100.0),   # 桨距角正常范围约 0–90，留裕量
    'yaw_position_canonical_abs_deg': 360.0, # 超出 ±360° 为累计角度/编码异常，原值保留
    'ambient_temp_physical_c': (-40.0, 50.0),
    'wind_speed_physical_ms': (0.0, 60.0),
    'state_step_h': 1.0 / 6.0,               # 单条记录覆盖一个 10 分钟区间
}

# 第一轮特征排除的列类别（原因随 quality_report 公开）。
ALL_MISSING_COLUMNS = ('有效风时数<=3(h)', '有效风时数>19(h)',
                       '有效风时数3-10(h)', '有效发电时间(h)')
ZERO_VARIANCE_COLUMNS = ('平均X轴方向加速度', '平均y轴方向加速度')
SENTINEL_ONLY_COLUMNS = SENTINEL_TEMP_COLUMNS
UNCONFIRMED_CHANNELS = ('平均偏航集电环温度(℃)',)  # 12.43% 缺失且有 >200℃ 异常

# 原始列 -> 规范字段映射（turbine_scada_10m 列；其余列仅存原始载荷）。
FIELD_MAP = {
    '统计时间': 'source_time_text',
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

STATE_H_FIELDS = ('normal_h', 'generating_h', 'fault_h',
                  'maintenance_h', 'env_abnormal_h', 'env_normal_h')


def parse_number(text):
    """'--'/空串/None -> (None, 'dash'|'empty')；数值文本 -> (float, None)。"""
    if text is None:
        return None, 'empty'
    s = str(text).strip()
    if not s:
        return None, 'empty'
    if s == '--':
        return None, 'dash'
    try:
        v = float(s)
    except ValueError:
        return None, 'unparseable'
    if not np.isfinite(v):
        return None, 'nonfinite'
    return v, None


def row_flags(values: dict, prev_time: datetime | None, observed_at: datetime) -> dict:
    """行级质量标记：只记录事实，不修改任何原值。

    values 为已解析的规范字段数值字典（None 表示缺失）。
    """
    flags: dict[str, float | int | str] = {}
    p = values.get('grid_active_power_kw')
    if p is not None:
        if p < 0:
            flags['negative_grid_power'] = p  # 真实净取用，保留原值
        elif p == 0:
            flags['zero_grid_power'] = 0
    for key, col in (('sentinel_847_intake_temp1', SENTINEL_TEMP_COLUMNS[0]),
                     ('sentinel_847_intake_temp2', SENTINEL_TEMP_COLUMNS[1])):
        v = values.get(f'_raw_{col}')
        if v is not None and v == SENTINEL_TEMP_847:
            flags[key] = SENTINEL_TEMP_847
    yaw_ring = values.get('_raw_平均偏航集电环温度(℃)')
    if yaw_ring is not None and yaw_ring > RULES['yaw_ring_temp_above_c']:
        flags['yaw_ring_temp_anomaly'] = yaw_ring
    lo, hi = RULES['pitch_canonical_deg']
    pitches = [values.get(k) for k in ('pitch1_deg', 'pitch2_deg', 'pitch3_deg')]
    bad_pitches = [v for v in pitches if v is not None and not (lo <= v <= hi)]
    if bad_pitches:
        # 记录偏离最远的越界叶片原值，供跨叶片一致性核查。
        flags['pitch_outside_canonical'] = min(
            bad_pitches, key=lambda v: min(abs(v - lo), abs(v - hi)))
    yaw = values.get('yaw_position_deg')
    if yaw is not None and abs(yaw) > RULES['yaw_position_canonical_abs_deg']:
        flags['yaw_position_outside_canonical'] = yaw
    amb = values.get('ambient_temp_c')
    lo, hi = RULES['ambient_temp_physical_c']
    if amb is not None and not (lo <= amb <= hi):
        flags['ambient_temp_outside_physical'] = amb
    wspd = values.get('nacelle_wind_speed_ms')
    lo, hi = RULES['wind_speed_physical_ms']
    if wspd is not None and not (lo <= wspd <= hi):
        flags['wind_speed_outside_physical'] = wspd
    long_states = {k: v for k, v in ((k, values.get(k)) for k in STATE_H_FIELDS)
                   if v is not None and v > RULES['state_step_h'] + 1e-9}
    if long_states:
        # 长状态时长可能汇总了缺失时段，须对照时间缺口，不单独判错。
        flags['state_duration_exceeds_step'] = max(long_states.values())
    if prev_time is not None and (observed_at - prev_time) != STEP:
        flags['after_time_gap'] = int((observed_at - prev_time).total_seconds() // 600)
    return flags


def time_axis_report(times: list[datetime]) -> dict:
    """10 分钟网格完整性：缺失点、缺口事件、最大缺口（只记录事实）。"""
    n = len(times)
    if n == 0:
        raise ValueError('No rows')
    deltas = [(b - a).total_seconds() / 60.0 for a, b in zip(times, times[1:])]
    expected = int((times[-1] - times[0]).total_seconds() // 600) + 1
    gap_events = [(times[i], times[i + 1], d) for i, d in enumerate(deltas) if d != 10.0]
    return {
        'start': str(times[0]), 'end': str(times[-1]),
        'rows': n,
        'expected_grid_points': expected,
        'missing_grid_points': expected - n,
        'duplicate_timestamps': n - len(set(times)),
        'monotonic': all(d > 0 for d in deltas),
        'gap_events': len(gap_events),
        'gap_delta_minutes_histogram': {
            str(int(d)): sum(1 for _, _, dd in gap_events if dd == d)
            for d in sorted({d for _, _, d in gap_events})},
        'max_gap_minutes': max((d for _, _, d in gap_events), default=0.0),
        'largest_gaps': [
            {'after': str(a), 'before': str(b), 'delta_minutes': d}
            for a, b, d in sorted(gap_events, key=lambda g: -g[2])[:10]],
    }


def column_profile(name: str, raw_values: list) -> dict:
    """列级画像：缺失、'--'、取值数、数值范围；不重建逐行数据。"""
    n = len(raw_values)
    dash = sum(1 for v in raw_values if str(v).strip() == '--')
    empty = sum(1 for v in raw_values if v is None or str(v).strip() == '')
    numbers, unparseable = [], 0
    for v in raw_values:
        if v is None or str(v).strip() in ('', '--'):
            continue
        try:
            numbers.append(float(str(v)))
        except ValueError:
            unparseable += 1
    profile = {'column': name, 'rows': n, 'dash_count': dash,
               'empty_count': empty, 'unparseable_count': unparseable,
               'value_count': len(numbers)}
    if name in SENTINEL_ONLY_COLUMNS:
        profile['sentinel_847_count'] = sum(1 for v in numbers if v == SENTINEL_TEMP_847)
    if numbers:
        profile['min'] = min(numbers)
        profile['max'] = max(numbers)
        profile['unique_count'] = len(set(numbers))
        if name in ZERO_VARIANCE_COLUMNS and profile['unique_count'] == 1:
            profile['zero_variance'] = True
    else:
        profile['unique_count'] = 0
        profile['all_missing'] = True
    profile['first_round_usage'] = _first_round_usage(name)
    return profile


def _first_round_usage(name: str) -> str:
    if name in FIELD_MAP:
        return 'typed_feature_or_label'
    if name in ALL_MISSING_COLUMNS:
        return 'excluded_all_missing'
    if name in ZERO_VARIANCE_COLUMNS:
        return 'excluded_zero_variance'
    if name in SENTINEL_ONLY_COLUMNS:
        return 'excluded_sentinel_only_channel'
    if name in UNCONFIRMED_CHANNELS:
        return 'excluded_unconfirmed_channel'
    if name in ('序号', '馈线名称', '设备名称', '型号名称'):
        return 'identity_metadata'
    return 'raw_payload_only'


def rule_documentation() -> dict:
    """规则卡片：单位、原始列、阈值与处理方式，供审计报告引用。"""
    return {
        'negative_grid_power': {
            'columns': ['平均网侧有功功率(kW)'], 'unit': 'kW',
            'rule': 'value < 0', 'handling': '保留原值入库并打标；不得截零或删行'},
        'zero_grid_power': {
            'columns': ['平均网侧有功功率(kW)'], 'unit': 'kW',
            'rule': 'value == 0', 'handling': '不视为缺失；结合状态时间检查'},
        'sentinel_847_intake_temp1/2': {
            'columns': list(SENTINEL_TEMP_COLUMNS), 'unit': '℃',
            'rule': 'value == 847（该两列仅有 0 与 847 两种取值）',
            'handling': '标记为疑似哨兵值；整列排除出第一轮特征，不当作温度'},
        'yaw_ring_temp_anomaly': {
            'columns': list(UNCONFIRMED_CHANNELS), 'unit': '℃',
            'rule': 'value > 200', 'handling': '标记异常；该通道另有 12.43% 缺失，整列暂不纳入特征'},
        'pitch_outside_canonical': {
            'columns': ['平均1#变桨桨距角(deg)', '平均2#变桨桨距角(deg)', '平均3#变桨桨距角(deg)'],
            'unit': 'deg', 'rule': '任一叶片超出 [-10, 100]',
            'handling': '保留原值；特征构造时该行桨距输入置为缺失（掩码），样本按严格可用性规则剔除'},
        'yaw_position_outside_canonical': {
            'columns': ['平均偏航位置(deg)'], 'unit': 'deg', 'rule': '|value| > 360',
            'handling': '保留原值；疑似累计角度，语义确认前不做取模/周期特征'},
        'state_duration_exceeds_step': {
            'columns': [k for k in FIELD_MAP.values() if k.endswith('_h')], 'unit': 'h',
            'rule': '任一状态时长 > 1/6 h', 'handling': '只记录；长记录可能汇总缺失时段，不判错'},
        'after_time_gap': {
            'columns': ['统计时间'], 'unit': 'minute',
            'rule': '与上一条记录差 ≠ 10 分钟', 'handling': '标记缺口后首行；样本构造要求完整 24h 历史窗口'},
    }
