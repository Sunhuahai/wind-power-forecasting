"""F01 原生 10 分钟基线模型：全部只用发布时刻已可见的历史输入。

标签为单机网侧 kW（含负值），预测不做 [0,∞) 裁剪：负值是真实的净取用状态，
截零会系统性美化停机时段误差。Ridge 的 alpha 与 LightGBM 早停只用验证集选择；
测试集不参与任何拟合、选择或早停（见 configs/f01_experiment.json 预注册）。
"""
from __future__ import annotations

import time as _time

import numpy as np
from lightgbm import LGBMRegressor, early_stopping
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from windpower.features.scada_samples import FEATURE_COLUMNS


class Persistence:
    """最后一个完整可见记录的网侧功率，对所有提前量恒定。"""
    def predict(self, x: np.ndarray) -> np.ndarray:
        return x[:, 0].copy()


class Seasonal:
    """同一目标时刻过去 7 天均值（构造期已回退处理）。"""
    def predict(self, x: np.ndarray) -> np.ndarray:
        return x[:, 0].copy()


def _val_mse(model, x, y) -> float:
    return float(np.mean((model.predict(x) - y) ** 2))


def make_bundles(train, validation, cfg) -> dict:
    """拟合四个基线；返回可 joblib 序列化的 bundle 注册表。"""
    tx = train.select(FEATURE_COLUMNS).to_numpy()
    ty = train['actual_kw'].to_numpy()
    vx = validation.select(FEATURE_COLUMNS).to_numpy()
    vy = validation['actual_kw'].to_numpy()
    bundles = {}

    started = _time.perf_counter()
    bundles['persistence'] = dict(
        name='persistence', rule='lag1', columns=['p_lag1'], model=Persistence(),
        metadata=dict(fit_seconds=round(_time.perf_counter() - started, 4)))
    bundles['seasonal'] = dict(
        name='seasonal', rule='same_target_time_7d_mean_with_lag1_fallback',
        columns=['seasonal_kw'], model=Seasonal(),
        metadata=dict(days=cfg['models']['seasonal']['days'],
                      fit_seconds=0.0))

    started = _time.perf_counter()
    candidates = []
    for alpha in cfg['models']['ridge']['alphas']:
        model = make_pipeline(StandardScaler(), Ridge(alpha=alpha))
        model.fit(tx, ty)
        candidates.append((_val_mse(model, vx, vy), alpha, model))
    mse, alpha, model = min(candidates, key=lambda t: t[0])
    bundles['ridge'] = dict(
        name='ridge', rule='standard_scaler_ridge', columns=FEATURE_COLUMNS, model=model,
        metadata=dict(alpha=alpha, validation_mse_selection=mse,
                      fit_seconds=round(_time.perf_counter() - started, 3)))

    params = dict(cfg['models']['lightgbm']['params'])
    rounds = cfg['models']['lightgbm']['early_stopping_rounds']
    started = _time.perf_counter()
    model = LGBMRegressor(**params)
    model.fit(tx, ty, eval_set=[(vx, vy)], callbacks=[early_stopping(rounds, verbose=False)])
    bundles['lightgbm'] = dict(
        name='lightgbm', rule='lgbm_early_stop_on_validation', columns=FEATURE_COLUMNS,
        model=model,
        metadata=dict(best_iteration=model.best_iteration_,
                      n_estimators_param=params['n_estimators'],
                      fit_seconds=round(_time.perf_counter() - started, 3)))
    return bundles


def predict(bundle: dict, frame) -> np.ndarray:
    x = frame.select(bundle['columns']).to_numpy()
    started = _time.perf_counter()
    values = np.asarray(bundle['model'].predict(x), float)
    elapsed = _time.perf_counter() - started
    bundle.setdefault('metadata', {})['last_predict_seconds'] = round(elapsed, 4)
    bundle['metadata']['last_predict_rows'] = int(len(values))
    return values
