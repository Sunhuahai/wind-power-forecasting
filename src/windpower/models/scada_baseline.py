"""F01 原生 10 分钟基线的端到端运行：入库数据 -> 样本 -> 模型 -> 验证评价 -> 报告。

预注册协议见 configs/f01_experiment.json：
- 训练集拟合；验证集用于 Ridge alpha 选择、LightGBM 早停与门禁判定；
- 基线和验证回放只读取验证截止前的观测。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
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
            [sys.executable, '-m', 'pytest', 'tests/test_scada_samples.py',
             'tests/test_scada_quality.py', '-q'],
            capture_output=True, text=True, timeout=600)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def _load_samples(turbine_id: int, cfg: dict, *, validation_only: bool):
    cutoff = datetime.fromisoformat(cfg['splits']['validation'][1]) if validation_only else None
    frame = load_turbine_frame(turbine_id, file_id=cfg['dataset']['file_sha256'],
                               observed_before=cutoff)
    return build_samples(frame, config_path=CONFIG_PATH, turbine_id=turbine_id)


def _has_test_evidence(out_dir: Path) -> bool:
    return (out_dir / 'test_manifest.json').exists() or (out_dir / 'test_summary.json').exists()


def run_final_test(turbine_id: int = 1, out_dir: Path = OUT_DIR,
                   split_name: str = 'test', write: bool = True) -> dict:
    """以保存的模型与训练阈值回放验证，或执行尚未记录的一次性测试评分。

    验证回放始终只返回结果，使用与 baseline-f01 相同的验证截止时间。
    测试评分要求验证门禁通过且没有已保存的测试证据。
    """
    if split_name not in ('validation', 'test'):
        raise ValueError('split must be validation or test')
    config_bytes = CONFIG_PATH.read_bytes()
    cfg = json.loads(config_bytes)
    out_dir = Path(out_dir)
    validation_only = split_name == 'validation'
    if not validation_only and (_has_test_evidence(OUT_DIR) or _has_test_evidence(out_dir)):
        raise ValueError('本协议的测试集已完成一次性评分；日常校验请使用 --split validation')
    summary_path = out_dir / 'summary.json'
    if not summary_path.is_file():
        raise FileNotFoundError(f'{summary_path} missing; run baseline-f01 --out-dir {out_dir} first')
    val_summary = json.loads(summary_path.read_text(encoding='utf-8'))
    if not validation_only and val_summary['gate']['status'] != 'pass':
        raise ValueError('验证门禁未通过；测试集必须保持锁定')
    bundles = {}
    for name in ('persistence', 'seasonal', 'ridge', 'lightgbm'):
        path = out_dir / 'models' / f'{name}.joblib'
        if not path.exists():
            raise FileNotFoundError(f'{path} missing; run baseline-f01 --out-dir {out_dir} first')
        bundles[name] = joblib.load(path)
    samples, coverage = _load_samples(turbine_id, cfg, validation_only=validation_only)
    part = split_by_target(samples, split_name, config_path=CONFIG_PATH)
    if part.is_empty():
        raise ValueError(f'Empty {split_name} split; check database ingestion')
    preds, runtimes = {}, {}
    for name, bundle in bundles.items():
        preds[name] = predict(bundle, part)
        runtimes[name] = dict(
            fit_seconds=bundle['metadata'].get('fit_seconds'),
            predict_seconds=bundle['metadata'].get('last_predict_seconds'))
    thresholds = dict(val_summary['thresholds'])
    results = evaluate_split(part, preds, runtimes, thresholds, cfg)
    if validation_only or not write:
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
        frozen_config_sha256=hashlib.sha256(config_bytes).hexdigest(),
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
    - 仅读取训练和验证所需的历史观测，覆盖统计同样止于验证期。
    """
    started = _time.perf_counter()
    out_dir = Path(out_dir)
    if _has_test_evidence(out_dir):
        raise ValueError(f'{out_dir} 已保存一次性测试证据；请用 --out-dir 指定新的运行目录')
    cfg = json.loads(CONFIG_PATH.read_text())
    samples, coverage = _load_samples(turbine_id, cfg, validation_only=True)
    train = split_by_target(samples, 'train', config_path=CONFIG_PATH)
    validation = split_by_target(samples, 'validation', config_path=CONFIG_PATH)
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
        for name, part in (('train', train), ('validation', validation))}
    coverage['observed_before'] = cfg['splits']['validation'][1]
    coverage['splits']['test'] = dict(rows=None, issues=None, target_min=None, target_max=None)
    test_availability = dict(
        status='locked_by_gate_this_round', rows=None, issues=None,
        note='验证运行未读取测试期观测，测试期覆盖率未统计。')

    summary = write_reports(out_dir, 'validation', results, thresholds, coverage, cfg,
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
        gate=summary['gate'],
        out=str(out_dir))
