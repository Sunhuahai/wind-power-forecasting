"""F01 单机评价：基础指标、分组诊断、爬坡、覆盖、运行耗时与政策资格控制。

所有模型在同一有效样本集合上配对比较；分组阈值与爬坡阈值只用训练集推导，
方法预注册于 configs/f01_experiment.json，不因验证/测试结果调整。
单机 10 分钟数据不具广西政策评价资格：政策字段输出 null 与原因，
不得以 0 分或其他虚构值替代（0 分是政策计算结果，缺数据是无法评价）。
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl

NaN = float('nan')


def basic_metrics(actual, prediction) -> dict:
    a = np.asarray(actual, float)
    p = np.asarray(prediction, float)
    error = p - a
    return dict(MAE=float(np.mean(np.abs(error))),
                RMSE=float(np.sqrt(np.mean(error ** 2))),
                Bias=float(np.mean(error)), n=int(len(error)))


def pooled(frame) -> dict:
    return basic_metrics(frame['actual_kw'], frame['prediction_kw'])


def per_lead(frame) -> list[dict]:
    rows = []
    for (lead,), part in frame.group_by(['lead_minutes']).__iter__():
        rows.append(dict(lead_minutes=int(lead), **basic_metrics(
            part['actual_kw'], part['prediction_kw'])))
    return sorted(rows, key=lambda r: r['lead_minutes'])


def per_day(frame) -> list[dict]:
    days = frame.with_columns(pl.col('valid_at').dt.date().alias('_day'))
    rows = []
    for (day,), part in sorted(days.group_by(['_day']).__iter__()):
        rows.append(dict(date=str(day),
                         n_issues=part['issue_at'].n_unique(),
                         **basic_metrics(part['actual_kw'], part['prediction_kw'])))
    return rows


def wind_groups(frame, bins: list[float]) -> list[dict]:
    edges = [-np.inf, *bins, np.inf]
    labels = []
    for i in range(len(edges) - 1):
        lo = '' if np.isneginf(edges[i]) else f"{edges[i]:g}"
        hi = '' if np.isinf(edges[i + 1]) else f"{edges[i+1]:g}"
        labels.append(f"{lo}~{hi}")
    out = []
    f = frame.drop_nulls('diag_wind_ms').filter(pl.col('diag_wind_ms').is_finite())
    if not f.height:
        return out
    codes = np.digitize(f['diag_wind_ms'].to_numpy(), bins, right=False)
    for i, label in enumerate(labels):
        part = f.filter(pl.Series(codes) == i)
        if part.height:
            out.append(dict(group=f'wind_ms_{label}', **basic_metrics(
                part['actual_kw'], part['prediction_kw'])))
    return out


def state_groups(frame) -> list[dict]:
    rules = {
        'generating': pl.col('diag_gen_h') > 0,
        'not_generating': pl.col('diag_gen_h') == 0,
        'fault': pl.col('diag_fault_h') > 0,
        'maintenance': pl.col('diag_maint_h') > 0,
        'env_abnormal': pl.col('diag_env_abn_h') > 0,
    }
    out = []
    for name, cond in rules.items():
        part = frame.filter(cond)
        if part.height:
            out.append(dict(group=f'state_{name}', **basic_metrics(
                part['actual_kw'], part['prediction_kw'])))
    return out


def power_groups(frame, train_max_kw: float, low_fraction: float, high_fraction: float) -> list[dict]:
    low_cut = low_fraction * train_max_kw
    high_cut = high_fraction * train_max_kw
    rules = {
        'negative': pl.col('actual_kw') < 0,
        'zero': pl.col('actual_kw') == 0,
        'low': (pl.col('actual_kw') > 0) & (pl.col('actual_kw') <= low_cut),
        'normal': (pl.col('actual_kw') > low_cut) & (pl.col('actual_kw') <= high_cut),
        'high': pl.col('actual_kw') > high_cut,
    }
    out = []
    for name, cond in rules.items():
        part = frame.filter(cond)
        if part.height:
            out.append(dict(group=f'power_{name}', **basic_metrics(
                part['actual_kw'], part['prediction_kw'])))
    return out


def ramp_report(frame, threshold_kw: float) -> dict:
    """爬坡事件按 |目标前 30 分钟实际变化| >= 训练集 95 分位阈值（预注册）。"""
    f = frame.drop_nulls('diag_dp30_kw')
    events = f.filter(pl.col('diag_dp30_kw').abs() >= threshold_kw)
    if not events.height:
        return dict(threshold_kw=threshold_kw, events=0)
    pred_change = np.asarray(events['prediction_kw'], float) - np.asarray(events['p_lag1'], float)
    actual_change = np.asarray(events['actual_kw'], float) - np.asarray(events['p_lag1'], float)
    direction = np.sign(pred_change) == np.sign(actual_change)
    return dict(threshold_kw=round(float(threshold_kw), 2), events=events.height,
                event_share=events.height / f.height,
                MAE_on_events=basic_metrics(events['actual_kw'], events['prediction_kw'])['MAE'],
                direction_accuracy=float(np.mean(direction)))


def train_thresholds(train_frame, cfg) -> dict:
    """只用训练集推导的预注册阈值：功率分组边界与爬坡阈值。"""
    ramp = cfg['evaluation']['ramp']
    # 30 分钟目标变化直接来自构造期保存的 diag_dp30_kw。
    changes = train_frame['diag_dp30_kw'].drop_nulls().abs()
    return dict(
        train_max_kw=float(train_frame['actual_kw'].max()),
        ramp_threshold_kw=float(changes.quantile(ramp['train_abs_change_quantile'])),
        ramp_quantile=ramp['train_abs_change_quantile'],
        low_cut_fraction=cfg['evaluation']['power_group_factors']['low_max_fraction'],
        high_cut_fraction=cfg['evaluation']['power_group_factors']['high_fraction'],
    )


def gate_check(summary_by_model: dict, tests_passed: bool, cfg) -> dict:
    """预注册验证门禁：本轮只报告状态，测试集保持关闭。"""
    if 'persistence' not in summary_by_model or 'lightgbm' not in summary_by_model:
        raise ValueError('Gate requires persistence and lightgbm results')
    pooled_better = (summary_by_model['lightgbm']['RMSE']
                     < summary_by_model['persistence']['RMSE'])
    def _lead_mean(v) -> float:
        leads = [r['RMSE'] for r in v['per_lead']]
        # 无逐提前量证据时不能声称改进。
        return float(np.mean(leads)) if leads else float('inf')
    lead_mean = {m: _lead_mean(v) for m, v in summary_by_model.items()}
    lead_better = lead_mean['lightgbm'] < lead_mean['persistence']
    criteria = {
        'c1_pooled_rmse_beats_persistence': bool(pooled_better),
        'c2_lead_mean_rmse_beats_persistence': bool(lead_better),
        'c3_leakage_and_protocol_tests': bool(tests_passed),
    }
    return dict(name=cfg['gate']['name'], criteria=criteria,
                status='pass' if all(criteria.values()) else 'fail',
                action_on_pass=cfg['gate']['action_on_pass'],
                test_set='locked_this_round')


def policy_block(cfg) -> dict:
    """单机数据政策资格：显式 null，不虚构分数。"""
    return dict(
        asset_level='turbine', target_source='single_turbine_scada_10m',
        eligible=cfg['policy_eligibility']['eligible'],
        GX_policy_accuracy=cfg['policy_eligibility']['GX_policy_accuracy'],
        ineligible_reason=cfg['policy_eligibility']['ineligible_reason'],
        rule_versions=cfg['policy_eligibility']['rule_versions'],
        note=('F01 为单机网侧 10 分钟数据，非场站并网点 15 分钟参考曲线，'
              '且缺容量证据、历史 NWP 与 16 批历史发布版本；'
              '取得真实场站资料后经 guangxi.py 回放，仍属研究性考核复现。'))


def _git_commit() -> str:
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return 'unknown'


def evaluate_split(frame, preds: dict, runtimes: dict, thresholds: dict, cfg) -> dict:
    """对一个数据分段做全模型评价；所有模型使用同一行集合。"""
    results = {}
    for name, pred in preds.items():
        if len(pred) != frame.height:
            raise ValueError(f'{name}: prediction length {len(pred)} != rows {frame.height}')
        scored = frame.with_columns(pl.Series('prediction_kw', np.asarray(pred, float)))
        rt = runtimes.get(name, {})
        summary = dict(
            model=name,
            **basic_metrics(scored['actual_kw'], scored['prediction_kw']),
            per_lead=per_lead(scored),
            per_day=per_day(scored),
            wind_groups=wind_groups(scored, cfg['evaluation']['wind_bins_ms']),
            state_groups=state_groups(scored),
            power_groups=power_groups(
                scored, thresholds['train_max_kw'],
                thresholds['low_cut_fraction'], thresholds['high_cut_fraction']),
            ramp=ramp_report(scored, thresholds['ramp_threshold_kw']),
            runtime=dict(
                fit_seconds=rt.get('fit_seconds'),
                predict_seconds=rt.get('predict_seconds'),
                per_issue_ms=round(1000 * rt.get('predict_seconds', 0)
                                   / max(scored['issue_at'].n_unique(), 1), 4)),
        )
        results[name] = summary
    return results


def write_reports(out: Path, split_name: str, results: dict, thresholds: dict,
                 coverage: dict, cfg, test_availability: dict,
                 tests_passed: bool | None, prefix: str = '',
                 gate_override: dict | None = None) -> dict:
    """统一报告输出：summary.json / per_lead.csv / per_day.csv / report.md。

    prefix 为空写验证轮文件名，'test_' 写测试轮文件。
    gate_override 用于一次性测试评分：测试结果只记录，不构成新的选择门禁。
    """
    out.mkdir(parents=True, exist_ok=True)
    lead_rows = [dict(model=m, **r) for m, v in results.items() for r in v['per_lead']]
    day_rows = [dict(model=m, **r) for m, v in results.items() for r in v['per_day']]
    pl.DataFrame(lead_rows).write_csv(out / f'{prefix}per_lead.csv')
    pl.DataFrame(day_rows).write_csv(out / f'{prefix}per_day.csv')
    gate = gate_override if gate_override else gate_check(results, tests_passed, cfg)
    summary = dict(
        task=cfg['task'],
        dataset=dict(file_sha256=cfg['dataset']['file_sha256'],
                     time_semantics=cfg['dataset']['time_semantics'],
                     interval_semantics=cfg['dataset']['interval_start_end_semantics']),
        availability_assumption=cfg['availability_assumption'],
        split=split_name,
        split_bounds=cfg['splits'][split_name],
        thresholds={k: (round(v, 3) if isinstance(v, float) else v)
                    for k, v in thresholds.items()},
        coverage=coverage,
        identical_sample_set=True,
        models={m: {k: v for k, v in s.items()} for m, s in results.items()},
        gate=gate,
        policy=policy_block(cfg),
        test_set=test_availability,
        generated_at=datetime.now(timezone.utc).isoformat(),
        code_commit=_git_commit(),
        config=cfg,
    )
    (out / f'{prefix}summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    _write_markdown(out, split_name, results, thresholds, coverage, gate, cfg,
                    prefix=prefix)
    return summary


def _write_markdown(out: Path, split_name: str, results: dict, thresholds: dict,
                    coverage: dict, gate: dict, cfg,
                    prefix: str = '') -> None:
    lines = [
        f'# F01 单机 10 分钟基线 · '
        f'{"测试集一次性最终评分" if prefix else split_name} 报告', '',
        f'- 数据：`F01号风机十分钟数据.csv`（sha256 `{cfg["dataset"]["file_sha256"][:16]}…`），'
        f'源时间为无时区时钟，区间起止语义未确认。',
        f'- 可见性假设：{cfg["availability_assumption"]["name"]}'
        f'（发布时刻 T 仅用 observed_at ≤ T−10min 的记录）。',
        f'- 标签：单机网侧有功功率 kW，负值保留不截零；预测同样不裁剪。', '',
        f'- 样本：{coverage.get("issues_valid", 0)} 个有效发布 × '
        f'{cfg["leads_steps"]} 个提前量（10—240 分钟）。',
        f'- 覆盖率：{coverage.get("coverage_share", 0):.2%}'
        f'（剔除：历史窗口不完整 {coverage.get("dropped_incomplete_history", 0)}，'
        f'特征缺失 {coverage.get("dropped_missing_feature", 0)}，'
        f'标签不完整 {coverage.get("dropped_incomplete_labels", 0)}）。', '',
        f'- 训练集推导阈值：最大功率 {thresholds["train_max_kw"]:.0f} kW；'
        f'爬坡阈值 {thresholds["ramp_threshold_kw"]:.0f} kW'
        f'（训练期 |ΔP30| 的 {int(thresholds["ramp_quantile"]*100)} 分位）。', '',
        '| 模型 | MAE (kW) | RMSE (kW) | Bias (kW) | n | 每发布耗时 (ms) |',
        '|---|---:|---:|---:|---:|---:|',
    ]
    for m, s in results.items():
        rt = s.get('runtime', {})
        lines.append(f"| {m} | {s['MAE']:.1f} | {s['RMSE']:.1f} | {s['Bias']:+.1f} "
                     f"| {s['n']} | {rt.get('per_issue_ms', 0.0):.2f} |")
    leads = {m: {r['lead_minutes']: r for r in s['per_lead']} for m, s in results.items()}
    lines += ['', '## 分提前量 RMSE (kW)', '',
              '| 提前量 | persistence | seasonal | ridge | lightgbm |', '|---:|---:|---:|---:|---:|']
    for lead in sorted(next(iter(leads.values())).keys()):
        row = [f'{lead} 分钟'] + [
            f"{leads[m][lead]['RMSE']:.1f}" if m in leads and lead in leads[m] else '—'
            for m in ('persistence', 'seasonal', 'ridge', 'lightgbm')]
        lines.append('| ' + ' | '.join(row) + ' |')
    best = min(results, key=lambda m: results[m]['RMSE'])
    lines += ['',
              f'## 分组与爬坡诊断（{best} 为例）', '']
    for g in results[best]['wind_groups']:
        lines.append(f"- 风速 {g['group']}：MAE {g['MAE']:.1f} kW（n={g['n']}）")
    for g in results[best]['state_groups']:
        lines.append(f"- 状态 {g['group']}：MAE {g['MAE']:.1f} kW（n={g['n']}）")
    for g in results[best]['power_groups']:
        lines.append(f"- 功率 {g['group']}：MAE {g['MAE']:.1f} kW（n={g['n']}）")
    ramp = results[best].get('ramp') or {}
    if ramp.get('events'):
        lines += ['',
                  f"- 爬坡事件（|ΔP30| ≥ {ramp.get('threshold_kw', 0):.0f} kW）："
                  f"{ramp.get('events', 0)} 个，事件内 MAE {ramp.get('MAE_on_events', 0):.1f} kW，"
                  f"方向准确率 {ramp.get('direction_accuracy', 0):.1%}。", '']
    else:
        lines += ['', '- 本分段无达到阈值的爬坡事件。', '']
    lines += ['## 政策评价资格', '',
              '- 单机 10 分钟 SCADA 非场站并网点 15 分钟参考曲线：'
              '`GX_policy_accuracy = null`（原因：single_turbine_10m_scada_is_not_farm_dispatch_reference）。',
              '- 取得真实场站计量、容量证据、历史 NWP 与发布批次后，'
              '经 `windpower.evaluation.guangxi` 做**研究性回放**，仍不等于调度认证成绩。', '']
    if prefix == 'test_':
        lines += ['## 一次性评分说明', '',
                  '- 模型、特征、阈值与超参数全部冻结自验证轮'
                  f'（门禁 `{gate["gate_reference"]}` 于 2026-11-21 通过）；'
                  '本报告为测试集唯一一次评分，此后不再依据测试结果做任何模型选择或调参。',
                  '- 记录性质对比（不构成新门禁）：见 summary.json 内各模型测试指标。', '']
    else:
        lines += ['## 测试门禁', '',
                  f"- 门禁 `{gate['name']}`：**{gate['status']}**（"
                  + '，'.join(f"{k}={v}" for k, v in gate['criteria'].items()) + '）。',
                  '- 本轮仅统计训练及验证期覆盖率，测试期观测未读取。', '']
    (out / f'{prefix}report.md').write_text('\n'.join(lines), encoding='utf-8')
