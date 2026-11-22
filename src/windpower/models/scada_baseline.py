"""F01 原生 10 分钟基线的端到端运行：入库数据 -> 样本 -> 模型 -> 验证评价 -> 报告。

预注册协议见 configs/f01_experiment.json：
- 训练集拟合；验证集用于 Ridge alpha 选择、LightGBM 早停与门禁判定；
- 测试集本轮保持关闭（规划 T04），仅报告可用样本规模，不计算任何测试指标。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time as _time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import joblib

from windpower.db.core import engine
from windpower.db.turbine_schema import feature_snapshot_manifest
from windpower.evaluation.scada_eval import (
    evaluate_split, train_thresholds, write_reports,
)
from windpower.features.scada_samples import (
    FEATURE_COLUMNS, build_samples, load_turbine_frame, split_by_target,
)
from windpower.models.scada_train import make_bundles, predict

CONFIG_PATH = Path('configs/f01_experiment.json')
OUT_DIR = Path('artifacts/f01')


def _protocol_tests_passed() -> bool | None:
    """门禁 c3：实际运行防泄漏与质量测试，取真实退出码。"""
    try:
        proc = subprocess.run(
            ['uv', 'run', 'pytest', 'tests/test_scada_samples.py',
             'tests/test_scada_quality.py', '-q'],
            capture_output=True, text=True, timeout=600)
        return proc.returncode == 0
    except Exception:
        return None


def run_final_test(turbine_id: int = 1, out_dir: Path = OUT_DIR,
                   split_name: str = 'test', write: bool = True) -> dict:
    """以冻结配置与冻结模型做一次性最终评分（预注册门禁通过后的唯一一次）。

    不重新训练：加载 baseline-f01 保存的模型 bundle；阈值原样复用验证轮
    summary.json 中由训练集推导的值。write=False 时只返回结果不落盘，
    用于在触碰测试标签前先用 validation 复现已存指标验证代码路径。
    """
    cfg = json.loads(CONFIG_PATH.read_text())
    out_dir = Path(out_dir)
    val_summary = json.loads((out_dir / 'summary.json').read_text())
    if val_summary['gate']['status'] != 'pass':
        raise ValueError('验证门禁未通过；测试集必须保持锁定')
    frame = load_turbine_frame(turbine_id)
    samples, coverage = build_samples(frame, turbine_id=turbine_id)
    part = split_by_target(samples, split_name)
    if part.is_empty():
        raise ValueError(f'Empty {split_name} split; check database ingestion')
    bundles = {}
    for name in ('persistence', 'seasonal', 'ridge', 'lightgbm'):
        path = out_dir / 'models' / f'{name}.joblib'
        if not path.exists():
            raise FileNotFoundError(f'{path} missing; run baseline-f01 first')
        bundles[name] = joblib.load(path)
    preds, runtimes = {}, {}
    for name, bundle in bundles.items():
        preds[name] = predict(bundle, part)
        runtimes[name] = dict(
            fit_seconds=bundle['metadata'].get('fit_seconds'),
            predict_seconds=bundle['metadata'].get('last_predict_seconds'))
    thresholds = dict(val_summary['thresholds'])
    results = evaluate_split(part, preds, runtimes, thresholds, cfg)
    if not write:
        return results
    coverage['splits'] = {split_name: dict(
        rows=part.height, issues=part['issue_at'].n_unique(),
        target_min=str(part['valid_at'].min()), target_max=str(part['valid_at'].max()))}
    gate_override = dict(
        name='ONE_TIME_FINAL_SCORING',
        status='recorded_no_further_selection',
        gate_reference='F01_FIRST_ROUND_VALIDATION_GATE pass (2026-11-21)',
        criteria=None,
        note='测试集按用户授权（2026-11-22）以冻结配置一次性评分；'
             '此后不依据测试结果做任何选择、调参或清洗调整。',
        validation_reference_rmse={m: v['RMSE'] for m, v in val_summary['models'].items()},
    )
    test_availability = dict(
        status='opened_one_time_final_scoring', rows=part.height,
        issues=part['issue_at'].n_unique(),
        authorized_by='user_2026-11-22',
        frozen_config_sha256=hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest(),
        snapshot_id=json.loads((out_dir / 'run_manifest.json').read_text())['snapshot_id'],
    )
    summary = write_reports(out_dir, split_name, results, thresholds, coverage, cfg,
                            test_availability, tests_passed=None, prefix='test_',
                            gate_override=gate_override)
    (out_dir / 'test_manifest.json').write_text(json.dumps(test_availability,
                                                           ensure_ascii=False, indent=2))
    return summary


def run_baseline(turbine_id: int = 1, out_dir: Path = OUT_DIR) -> dict:
    """F01 原生 10 分钟基线的端到端运行：入库数据 -> 样本 -> 模型 -> 验证评价 -> 报告。

    预注册协议见 configs/f01_experiment.json：
    - 训练集拟合；验证集用于 Ridge alpha 选择、LightGBM 早停与门禁判定；
    - 测试集首轮保持关闭（规划 T04），一次性最终评分见 run_final_test。
    """
    started = _time.perf_counter()
    cfg = json.loads(CONFIG_PATH.read_text())
    frame = load_turbine_frame(turbine_id)
    samples, coverage = build_samples(frame, turbine_id=turbine_id)
    train = split_by_target(samples, 'train')
    validation = split_by_target(samples, 'validation')
    test = split_by_target(samples, 'test')
    if train.is_empty() or validation.is_empty():
        raise ValueError('Empty train/validation split; check database ingestion')
    thresholds = train_thresholds(train, cfg)

    bundles = make_bundles(train, validation, cfg)
    preds, runtimes = {}, {}
    for name, bundle in bundles.items():
        preds[name] = predict(bundle, validation)
        runtimes[name] = dict(
            fit_seconds=bundle['metadata'].get('fit_seconds'),
            predict_seconds=bundle['metadata'].get('last_predict_seconds'))
    results = evaluate_split(validation, preds, runtimes, thresholds, cfg)
    tests_passed = _protocol_tests_passed()

    coverage['splits'] = {
        name: dict(rows=part.height, issues=part['issue_at'].n_unique(),
                   target_min=str(part['valid_at'].min()), target_max=str(part['valid_at'].max()))
        for name, part in (('train', train), ('validation', validation), ('test', test))}
    test_availability = dict(
        status='locked_by_gate_this_round', rows=test.height,
        issues=test['issue_at'].n_unique(),
        note='测试集未评分；打开须在下一轮以本冻结配置执行一次性最终评分。')

    out_dir = Path(out_dir)
    write_reports(out_dir, 'validation', results, thresholds, coverage, cfg,
                  test_availability, tests_passed)

    (out_dir / 'models').mkdir(parents=True, exist_ok=True)
    for name, bundle in bundles.items():
        joblib.dump(bundle, out_dir / 'models' / f'{name}.joblib')

    snapshot_id = str(uuid.uuid4())
    manifest_row = dict(
        snapshot_id=snapshot_id, task=cfg['task'],
        dataset_file_id=cfg['dataset']['file_sha256'], turbine_id=turbine_id,
        created_at=datetime.now(timezone.utc),
        availability_assumption=cfg['availability_assumption']['name'],
        config=cfg, feature_columns=FEATURE_COLUMNS,
    )
    with engine().begin() as conn:
        conn.execute(feature_snapshot_manifest.insert().values(manifest_row))
    (out_dir / 'run_manifest.json').write_text(json.dumps(dict(
        snapshot_id=snapshot_id,
        dataset_file_sha256=cfg['dataset']['file_sha256'],
        turbine_id=turbine_id,
        config=str(CONFIG_PATH),
        availability_assumption=cfg['availability_assumption'],
        test_set_status=test_availability['status'],
        protocol_tests_passed=tests_passed,
        wall_seconds=round(_time.perf_counter() - started, 1),
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), ensure_ascii=False, indent=2))
    return dict(
        snapshot_id=snapshot_id,
        models={m: {k: s[k] for k in ('MAE', 'RMSE', 'Bias', 'n')}
                for m, s in results.items()},
        coverage=coverage['splits'],
        out=str(out_dir))
