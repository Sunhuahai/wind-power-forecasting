"""原生 10 分钟单机预测输入输出测试：基线模型的封闭算例（T04）。"""
import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from tests.synthetic_scada import frame, tiny_config
from windpower.features.scada_samples import (
    BASELINE_COLUMNS, FEATURE_COLUMNS, build_samples, split_by_target,
)
from windpower.models import scada_train


@pytest.fixture()
def parts(tmp_path):
    cfg_path = tiny_config(tmp_path / 'synth_config.json')
    cfg = json.loads(cfg_path.read_text())
    samples, _ = build_samples(frame(700), config_path=cfg_path)
    return cfg, split_by_target(samples, 'train', config_path=cfg_path), \
        split_by_target(samples, 'validation', config_path=cfg_path)


def test_persistence_repeats_last_visible_power(parts):
    cfg, train, validation = parts
    bundles = scada_train.make_bundles(train, validation, cfg)
    pred = scada_train.predict(bundles['persistence'], validation)
    assert np.allclose(pred, validation['p_lag1'].to_numpy())
    assert pred.shape == (validation.height,)


def test_seasonal_uses_precomputed_reference(parts):
    cfg, train, validation = parts
    bundles = scada_train.make_bundles(train, validation, cfg)
    pred = scada_train.predict(bundles['seasonal'], validation)
    assert np.allclose(pred, validation['seasonal_kw'].to_numpy())


def test_ridge_and_lightgbm_predict_finite_without_clipping(parts):
    cfg, train, validation = parts
    bundles = scada_train.make_bundles(train, validation, cfg)
    for name in ('ridge', 'lightgbm'):
        pred = scada_train.predict(bundles[name], validation)
        assert pred.shape == (validation.height,)
        assert np.isfinite(pred).all()
    assert bundles['ridge']['metadata']['alpha'] in cfg['models']['ridge']['alphas']
    assert 0 < bundles['lightgbm']['metadata']['best_iteration'] \
        <= cfg['models']['lightgbm']['params']['n_estimators']


def test_all_models_use_the_same_sample_rows(parts):
    cfg, train, validation = parts
    bundles = scada_train.make_bundles(train, validation, cfg)
    preds = {n: scada_train.predict(b, validation) for n, b in bundles.items()}
    for name, pred in preds.items():
        assert len(pred) == validation.height
    assert set(preds) == {'persistence', 'seasonal', 'ridge', 'lightgbm'}


def test_baseline_columns_disjoint_from_model_features():
    """p_lag1 同时是持续基线输出与学习特征（合法复用）；季节基线输出、
    标签与诊断列不得进入学习模型特征。"""
    assert 'seasonal_kw' not in FEATURE_COLUMNS
    for col in FEATURE_COLUMNS:
        assert not col.startswith(('diag_', 'actual'))
    assert 'valid_at' not in FEATURE_COLUMNS and 'issue_at' not in FEATURE_COLUMNS
    assert 'lead_step' in FEATURE_COLUMNS
