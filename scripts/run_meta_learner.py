"""Learned meta-learner over the accepted components, fitted on validation only.

Hypothesis: the final combination is a chain of convex blends with global
weights; a small gradient-boosted meta-learner that sees every component
prediction plus regime context (lead, site, hour, wind level, cross-site
spread) can weight components per regime and beat the fixed chain.
"""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor, early_stopping
from sklearn.preprocessing import StandardScaler

from windpower.features.build import build
from windpower.features.cross_nwp import spatial_panel, attach_spatial, temporal_spatial_panel, attach_temporal, COLUMNS, TEMPORAL_COLUMNS
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
OUT = PARENT / 'meta_learner_v1'
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
MLP_FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS
SPATIAL_FEATURES = kp.STAGE2 + COLUMNS
PARAMS = dict(num_leaves=15, min_child_samples=200, learning_rate=0.05, n_estimators=600,
              reg_lambda=1.0, random_state=42, n_jobs=2, verbosity=-1)
META = ['C', 'neighbors', 'full', 'spatial', 'temporal', 'shared', 'curve', 'mlp',
        'lead', 'site', 'hour', 'speed100', 'speed_mean', 'speed_spread']


def mlp_design(frame, columns):
    farms = frame['farm'].to_numpy().astype(int)
    return np.column_stack([frame.select(columns).to_numpy(), np.eye(10)[farms - 1]])


def main():
    registry = json.loads(Path('docs/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id'] != 'mlp_seedavg_tuned':
        raise ValueError('Baseline changed; refusing to run')
    OUT.mkdir(exist_ok=False)
    cfg = config()
    protocol = dict(
        method='Validation-fitted LightGBM meta-learner over component predictions and regime context',
        meta_features=META, params=PARAMS,
        selection='Rounds chosen by 5-fold contiguous time-block CV inside validation; then refit on all validation rows',
        caveat='Meta-learner training set is the 2013 H1 validation used for all earlier selection; test scored once')

    stage1 = joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib')))['stage1']
    s_models = {s: joblib.load(PARENT / 'spatial_nwp_v1' / f'site_{s}.joblib') for s in range(1, 11)}
    mlp_bundle = joblib.load(PARENT / 'mlp_seedavg_tuned_v1/model.joblib')
    mlp_columns = mlp_bundle['columns']

    panels, parts, oofs, crosses, val_parts, test_parts = {}, {}, [], [], [], []
    for site in range(1, 11):
        panel = kp.nwp_panel(site)
        panels[site] = panel
        parts[site] = {k: kp.attach(v, panel) for k, v in partitions(build(site)).items()}
        oofs.append(kp.stage1_oof(parts[site]['train'], panel, cfg['lightgbm'], site))
        crosses.append(panel.select('origin', 'valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()), 0, 1))))
        print(f'Prepared zone {site}', flush=True)
    s_panel, t_panel = spatial_panel(panels), temporal_spatial_panel(panels)
    cross_train, cross_future = kp.combine(oofs), kp.combine(crosses)

    val_frames, test_frames = [], []
    for site in range(1, 11):
        va = kp.add_cross_site(parts[site]['validation'], cross_future).sort('origin', 'valid_at')
        te = kp.add_cross_site(parts[site]['test'], cross_future).sort('origin', 'valid_at')
        va_s = attach_spatial(va, s_panel)
        te_s = attach_spatial(te, s_panel)
        spat_v = np.clip(s_models[site]['models'][0].predict(va_s.select(SPATIAL_FEATURES).to_numpy()), 0, 1)
        spat_t = np.clip(s_models[site]['models'][0].predict(te_s.select(SPATIAL_FEATURES).to_numpy()), 0, 1)
        va_t = attach_temporal(va, t_panel).with_columns(pl.lit(site).alias('farm'))
        te_t = attach_temporal(te, t_panel).with_columns(pl.lit(site).alias('farm'))
        mlp_v = np.clip(np.mean([m.predict(mlp_design(va_t, mlp_columns)) for m in mlp_bundle['models']], axis=0), 0, 1)
        mlp_t = np.clip(np.mean([m.predict(mlp_design(te_t, mlp_columns)) for m in mlp_bundle['models']], axis=0), 0, 1)
        val_frames.append(va.select([k for k in KEYS if k != 'site'] + ['speed100']).with_columns(
            pl.lit(site).alias('site'), pl.Series('spatial', spat_v), pl.Series('mlp', mlp_v)))
        test_frames.append(te.select([k for k in KEYS if k != 'site'] + ['speed100']).with_columns(
            pl.lit(site).alias('site'), pl.Series('spatial', spat_t), pl.Series('mlp', mlp_t)))
    val = pl.concat(val_frames)
    test = pl.concat(test_frames)

    def attach_components(frame, split):
        out = frame
        if split == 'val':
            frames = []
            for comp in ('C', 'neighbors', 'full'):
                for site in range(1, 11):
                    path = Path(f'artifacts/kpower_reproduction/{ID}_{comp}/site_{site}/validation_predictions.csv')
                    frames.append(pl.read_csv(path, try_parse_dates=True)
                                  .select(['origin', 'valid_at', 'prediction'])
                                  .with_columns(pl.lit(site).alias('site'))
                                  .rename({'prediction': comp}))
            out = out.join(pl.concat(frames), on=KEYS, how='inner', validate='1:1')
        else:
            comps = pl.read_csv(PARENT / 'paired_predictions.csv', try_parse_dates=True) \
                .select(KEYS + ['C', 'neighbors', 'full'])
            out = out.join(comps, on=KEYS, how='inner', validate='1:1')
        for comp, folder, column in (('temporal', 'temporal_nwp_v1', 'temporal'),
                                     ('shared', 'shared_model_v1', 'shared'),
                                     ('curve', 'curve_residual_v1', 'curve_residual')):
            path = PARENT / folder / ('validation_predictions.csv' if split == 'val' else 'predictions.csv')
            frame = pl.read_csv(path, try_parse_dates=True).select(KEYS + [column]).rename({column: comp})
            out = out.join(frame, on=KEYS, how='inner', validate='1:1')
        speed = s_panel.select(['origin', 'valid_at'] + [f'nwp_{s}_speed100' for s in range(1, 11)])
        out = out.join(speed, on=['origin', 'valid_at'], how='inner', validate='1:1').with_columns(
            pl.concat_list([pl.col(f'nwp_{s}_speed100') for s in range(1, 11)]).list.mean().alias('speed_mean'),
            pl.concat_list([pl.col(f'nwp_{s}_speed100') for s in range(1, 11)]).list.std().alias('speed_spread'),
            pl.col('valid_at').dt.hour().alias('hour'))
        return out.drop([f'nwp_{s}_speed100' for s in range(1, 11)])

    val = attach_components(val, 'val')
    test = attach_components(test, 'test')
    if val.height != 43008 or test.height != 42432:
        raise ValueError(f'Unexpected row counts: {val.height} {test.height}')

    x_va, y_va = val.select(META).to_numpy(), val['actual'].to_numpy()
    x_te, y_te = test.select(META).to_numpy(), test['actual'].to_numpy()
    origins = val['origin'].unique().sort().to_list()
    folds = np.array_split(np.array(range(len(origins))), 5)
    best_iters = []
    for i, fold in enumerate(folds):
        hold = set(origins[fold[0]:fold[-1] + 1])
        mask_hold = val['origin'].is_in(list(hold)).to_numpy()
        model = LGBMRegressor(**PARAMS)
        model.fit(x_va[~mask_hold], y_va[~mask_hold],
                  eval_set=[(x_va[mask_hold], y_va[mask_hold])],
                  callbacks=[early_stopping(50, verbose=False)])
        best_iters.append(int(model.best_iteration_))
        print(f'fold {i} rounds={model.best_iteration_}', flush=True)
    rounds = int(np.median(best_iters))
    model = LGBMRegressor(**dict(PARAMS, n_estimators=rounds))
    model.fit(x_va, y_va)
    joblib.dump(dict(model=model, columns=META, protocol=protocol), OUT / 'model.joblib')
    (OUT / 'protocol.json').write_text(json.dumps(protocol, indent=2))

    previous = pl.read_csv(PARENT / 'mlp_seedavg_tuned_v1/predictions.csv', try_parse_dates=True)
    joined = test.join(previous.select(KEYS + ['mlp_seedavg_blend']), on=KEYS, how='inner', validate='1:1')
    if joined.height != 42432:
        raise ValueError('Test keys differ')
    candidate = np.clip(model.predict(x_te), 0, 1)
    result = joined.select(KEYS).with_columns(
        pl.Series('baseline', joined['mlp_seedavg_blend']),
        pl.Series('meta', candidate))
    scores = [dict(model=n, **metrics(result['actual'], result[n])) for n in ['baseline', 'meta']]
    paired = paired_comparison(result, 'meta')
    accepted = scores[-1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    (OUT / 'weights.json').write_text(json.dumps(dict(cv_rounds=best_iters, rounds=rounds,
        validation_rows=val.height, test_rows=test.height), indent=2))
    pl.DataFrame(scores).write_csv(OUT / 'metrics.csv')
    (OUT / 'paired.json').write_text(json.dumps(paired, indent=2))
    (OUT / 'decision.json').write_text(json.dumps(dict(accepted=accepted, scores=scores), indent=2))
    if accepted:
        result.write_csv(OUT / 'predictions.csv')
    print(json.dumps(dict(accepted=accepted, scores=scores, paired=paired), indent=2), flush=True)


if __name__ == '__main__':
    main()
