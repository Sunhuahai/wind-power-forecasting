"""F01 原生 10 分钟滚动预测样本：严格 as-of 构造，防未来信息泄漏。

发布时刻 T（10 分钟网格）只允许使用 observed_at <= T - visibility_delay 的记录
（configs/f01_experiment.json 的 OFFLINE_REPLAY_COMPLETED_INTERVAL 假设）。
该规则在"区间起点平均"与"区间终点平均"两种未确认语义下都不产生泄漏：
- 若统计时间=t 为区间起点，记录在 t+10 才完整，T 时可见的最后一格为 T-10；
- 若统计时间=t 为区间终点，记录在 t 已完整，T-10 格更早可见。
目标标签为统计时间=T+lead 的网侧功率记录；同一目标可对应多个发布时刻。
评估诊断列（diag_*）只用于分组报告，模型输入列显式声明，二者物理隔离。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from sqlalchemy import text

from windpower.db.core import engine

STEP = timedelta(minutes=10)

# 模型输入列（历史可见）；diag_* 与 actual_* 禁止进入此清单。
POWER_LAGS = [1, 2, 3, 6, 12, 36, 72, 144]
WIND_LAGS = [1, 6, 144]
FEATURE_COLUMNS = (
    [f'p_lag{k}' for k in POWER_LAGS]
    + ['p_mean144', 'p_std144', 'p_min144', 'p_max144',
       'p_chg_1_2', 'p_chg_1_6', 'p_chg_1_144']
    + [f'w_lag{k}' for k in WIND_LAGS] + ['w_mean144', 'w_std144']
    + ['t_lag1', 't_lag144', 'rotor_lag1', 'rotor_lag2', 'gen_lag1',
       'pitch_mean_lag1', 'gen_h_lag1', 'fault_h_lag1', 'maint_h_lag1',
       'env_abn_h_lag1', 'normal_h_lag1', 'env_norm_h_lag1',
       'hour_sin', 'hour_cos', 'dow_sin', 'dow_cos', 'doy_sin', 'doy_cos',
       'lead_step'])
BASELINE_COLUMNS = ['p_lag1', 'seasonal_kw']


def load_turbine_frame(turbine_id: int = 1) -> pl.DataFrame:
    """从数据库读取规范 10 分钟行（单一 file 版本），按时间升序。"""
    with engine().connect() as conn:
        rows = conn.execute(text(
            'SELECT observed_at, grid_active_power_kw, nacelle_wind_speed_ms,'
            ' ambient_temp_c, rotor_rpm, generator_rpm, pitch1_deg, pitch2_deg,'
            ' pitch3_deg, generating_h, fault_h, maintenance_h, normal_h,'
            ' env_abnormal_h, env_normal_h, file_id FROM turbine_scada_10m'
            ' WHERE turbine_id=:t ORDER BY observed_at'), {'t': turbine_id}).mappings().all()
    if not rows:
        raise ValueError('turbine_scada_10m empty; run import-f01 first')
    return pl.DataFrame([dict(r) for r in rows]).sort('observed_at')


def _masked(value, lo, hi):
    return value if value is not None and lo <= value <= hi else np.nan


def build_samples(frame: pl.DataFrame, config_path: str | Path = 'configs/f01_experiment.json',
                  turbine_id: int = 1) -> tuple[pl.DataFrame, dict]:
    """构造 (issue, lead) 样本矩阵；返回 (样本表, 覆盖统计)。

    纯函数，不访问数据库；特征只来自发布时刻已可见的历史记录。
    """
    cfg = json.loads(Path(config_path).read_text())
    vis_delay = timedelta(minutes=cfg['availability_assumption']['visibility_delay_minutes'])
    n_leads = cfg['leads_steps']
    hist_steps = cfg['history']['required_complete_steps']
    masks = cfg['history']['feature_mask_rules']
    pitch_lo, pitch_hi = masks['pitch_deg']
    amb_lo, amb_hi = masks['ambient_temp_c']
    wspd_lo, wspd_hi = masks['nacelle_wind_speed_ms']
    seasonal_days = cfg['models']['seasonal']['days']

    records: dict[datetime, dict] = {}
    for r in frame.iter_rows(named=True):
        records[r['observed_at']] = r
    times = sorted(records)
    t0, t1 = times[0], times[-1]

    rows = []
    stats = dict(issues_total=0, issues_valid=0, dropped_incomplete_history=0,
                 dropped_missing_feature=0, dropped_incomplete_labels=0)
    first_issue = t0 + vis_delay + timedelta(minutes=10 * (hist_steps - 1))
    last_issue = t1 - timedelta(minutes=10 * n_leads)
    issue = first_issue
    while issue <= last_issue:
        stats['issues_total'] += 1
        window = []
        ok = True
        for j in range(hist_steps):
            t = issue - vis_delay - timedelta(minutes=10 * j)
            r = records.get(t)
            if r is None or r['grid_active_power_kw'] is None:
                ok = False
                break
            window.append(r)
        if not ok:
            stats['dropped_incomplete_history'] += 1
            issue += STEP
            continue
        # window[0] = 最近可见记录（lag1），window[j] = lag(j+1)。
        power = np.array([r['grid_active_power_kw'] for r in window], float)
        winds = np.array([_masked(r['nacelle_wind_speed_ms'], wspd_lo, wspd_hi) for r in window])
        pitch_mean_lag1 = np.mean([_masked(window[0][k], pitch_lo, pitch_hi)
                                   for k in ('pitch1_deg', 'pitch2_deg', 'pitch3_deg')])
        base = dict(
            p_lag1=power[0], p_lag2=power[1], p_lag3=power[2],
            p_lag6=power[5], p_lag12=power[11], p_lag36=power[35],
            p_lag72=power[71], p_lag144=power[143],
            p_mean144=power.mean(), p_std144=power.std(), p_min144=power.min(),
            p_max144=power.max(), p_chg_1_2=power[0] - power[1],
            p_chg_1_6=power[0] - power[5], p_chg_1_144=power[0] - power[143],
            w_lag1=winds[0], w_lag6=winds[5], w_lag144=winds[143],
            w_mean144=np.nanmean(winds), w_std144=np.nanstd(winds),
            t_lag1=_masked(window[0]['ambient_temp_c'], amb_lo, amb_hi),
            t_lag144=_masked(window[143]['ambient_temp_c'], amb_lo, amb_hi),
            rotor_lag1=window[0]['rotor_rpm'], rotor_lag2=window[1]['rotor_rpm'],
            gen_lag1=window[0]['generator_rpm'], pitch_mean_lag1=pitch_mean_lag1,
            gen_h_lag1=window[0]['generating_h'], fault_h_lag1=window[0]['fault_h'],
            maint_h_lag1=window[0]['maintenance_h'], env_abn_h_lag1=window[0]['env_abnormal_h'],
            normal_h_lag1=window[0]['normal_h'], env_norm_h_lag1=window[0]['env_normal_h'],
        )
        hour_angle = 2 * np.pi * (issue.hour + issue.minute / 60.0) / 24.0
        dow_angle = 2 * np.pi * issue.weekday() / 7.0
        doy_angle = 2 * np.pi * (issue.timetuple().tm_yday - 1) / 365.25
        base.update(hour_sin=np.sin(hour_angle), hour_cos=np.cos(hour_angle),
                    dow_sin=np.sin(dow_angle), dow_cos=np.cos(dow_angle),
                    doy_sin=np.sin(doy_angle), doy_cos=np.cos(doy_angle))
        feature_ok = all(np.isfinite(v) for v in base.values())
        if not feature_ok:
            stats['dropped_missing_feature'] += 1
            issue += STEP
            continue
        targets = [(issue + timedelta(minutes=10 * k), k) for k in range(1, n_leads + 1)]
        if any(t not in records or records[t]['grid_active_power_kw'] is None
               for t, _ in targets):
            stats['dropped_incomplete_labels'] += 1
            issue += STEP
            continue
        stats['issues_valid'] += 1
        # lead k ∈ 1..24：目标时间 = issue + 10k 分钟，提前量 = 10k 分钟。
        for t, k in targets:
            row = dict(base)
            row.update(issue_at=issue, valid_at=t, lead_minutes=10 * k, lead_step=k,
                       turbine_id=turbine_id)
            label = records[t]
            # 基线输入：仅由可见历史推导。
            refs, missing = [], 0
            for d in range(1, seasonal_days + 1):
                ref_t = t - timedelta(days=d)
                ref = records.get(ref_t)
                if ref is None or ref['grid_active_power_kw'] is None:
                    missing += 1
                else:
                    refs.append(ref['grid_active_power_kw'])
            row['seasonal_kw'] = float(np.mean(refs)) if refs else base['p_lag1']
            row['seasonal_days_used'] = seasonal_days - missing
            # 标签与评估诊断列：模型输入禁止使用。
            row['actual_kw'] = label['grid_active_power_kw']
            row['diag_wind_ms'] = label['nacelle_wind_speed_ms']
            row['diag_gen_h'] = label['generating_h']
            row['diag_fault_h'] = label['fault_h']
            row['diag_maint_h'] = label['maintenance_h']
            row['diag_env_abn_h'] = label['env_abnormal_h']
            prev30 = records.get(t - timedelta(minutes=30))
            row['diag_dp30_kw'] = (label['grid_active_power_kw']
                                   - prev30['grid_active_power_kw']
                                   if prev30 is not None and prev30['grid_active_power_kw'] is not None
                                   else None)
            rows.append(row)
        issue += STEP
    if not rows:
        raise ValueError('No valid samples; check history completeness and split range')
    stats['coverage_share'] = stats['issues_valid'] / max(stats['issues_total'], 1)
    return pl.DataFrame(rows).sort(['issue_at', 'lead_minutes']), stats


def split_by_target(frame: pl.DataFrame, name: str,
                    config_path: str | Path = 'configs/f01_experiment.json') -> pl.DataFrame:
    """按目标时间（valid_at）左闭右开划分；同一目标标签只属于一个分段。"""
    cfg = json.loads(Path(config_path).read_text())
    start, end = (datetime.fromisoformat(v) for v in cfg['splits'][name])
    return frame.filter((pl.col('valid_at') >= start) & (pl.col('valid_at') < end))
