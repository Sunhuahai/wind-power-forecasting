"""Cross-site wind-field coherence aggregates for a per-site stage-two model.

Pre-registration (see protocol.json):
  added features : speed_mean, speed_std, speed_min, speed_max, speed_range,
                   dir_coherence, speed_dev  (second layer only)
  training data  : 2012 OOF panel (65,760 rows), nothing else
  early stopping : 2013 H1 validation
  blend weights  : 2013 H1 validation
  test           : scored once, acceptance needs RMSE down and 7-day paired CI upper < 0
"""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor, early_stopping
from sklearn.neural_network import MLPRegressor

from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel, attach_temporal, COLUMNS, TEMPORAL_COLUMNS
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.models.validation_blend import fit_weights
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
OUT = PARENT / 'dispersion_features_v1'
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
AGGS = ['speed_mean', 'speed_std', 'speed_min', 'speed_max', 'speed_range', 'dir_coherence']
FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS + AGGS + ['speed_dev']
MLP_FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS


def coherence(panels):
    cols = [f's{s}' for s in range(1, 11)]
    keys = panels[1].select('origin', 'valid_at')
    speed = keys.with_columns([panels[s]['speed100'].alias(f's{s}') for s in range(1, 11)])
    frame = speed.with_columns([
        pl.concat_list([pl.col(c) for c in cols]).list.mean().alias('speed_mean'),
        pl.concat_list([pl.col(c) for c in cols]).list.std().alias('speed_std'),
        pl.concat_list([pl.col(c) for c in cols]).list.min().alias('speed_min'),
        pl.concat_list([pl.col(c) for c in cols]).list.max().alias('speed_max'),
    ])
    frame = frame.with_columns((pl.col('speed_max') - pl.col('speed_min')).alias('speed_range'))
    u_mean = keys.with_columns([panels[s]['u100'].alias(f'u{s}') for s in range(1, 11)]) \
        .select(pl.concat_list([pl.col(f'u{s}') for s in range(1, 11)]).list.mean().alias('u_mean'))
    v_mean = keys.with_columns([panels[s]['v100'].alias(f'v{s}') for s in range(1, 11)]) \
        .select(pl.concat_list([pl.col(f'v{s}') for s in range(1, 11)]).list.mean().alias('v_mean'))
    frame = frame.with_columns((u_mean['u_mean'] ** 2 + v_mean['v_mean'] ** 2).sqrt().alias('dir_coherence'))
    dev = {s: keys.with_columns((panels[s]['speed100'] - frame['speed_mean']).alias('speed_dev'))
           for s in range(1, 11)}
    return frame.select(['origin', 'valid_at'] + AGGS), dev


def mlp_design(frame, columns):
    farms = frame['farm'].to_numpy().astype(int)
    return np.column_stack([frame.select(columns).to_numpy(), np.eye(10)[farms - 1]])


def main():
    registry = json.loads(Path('docs/研究记录/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id'] != 'mlp_seedavg_tuned':
        raise ValueError('Baseline changed; refusing to run')
    OUT.mkdir(exist_ok=False)
    cfg = config()
    params = dict(cfg['lightgbm'], num_leaves=31, min_child_samples=80, n_estimators=600)
    protocol = dict(
        method='Cross-site wind-field coherence aggregates added to a per-site second-layer model',
        added_features=AGGS + ['speed_dev'], layer='second layer, per site', features=FEATURES,
        params=params,
        training_data='2012 OOF panel only (65,760 rows)',
        early_stopping='2013 H1 validation',
        blend_weights='2013 H1 validation, convex between current best and new component',
        test_scoring='single pass, no tuning',
        acceptance='RMSE below 0.156009848 and 7-day block paired 95% CI upper < 0',
        caveat='Exploratory historical test; NWP issue times unknown')
    (OUT / 'protocol.json').write_text(json.dumps(protocol, indent=2))

    stage1 = joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib')))['stage1']
    mlp_bundle = joblib.load(PARENT / 'mlp_seedavg_tuned_v1/model.joblib')
    panels, parts, oofs, crosses = {}, {}, [], []
    for site in range(1, 11):
        panel = kp.nwp_panel(site)
        panels[site] = panel
        parts[site] = {k: kp.attach(v, panel) for k, v in partitions(build(site)).items()}
        oofs.append(kp.stage1_oof(parts[site]['train'], panel, cfg['lightgbm'], site))
        crosses.append(panel.select('origin', 'valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()), 0, 1))))
        print(f'Prepared zone {site}', flush=True)
    t_panel = temporal_spatial_panel(panels)
    agg, dev = coherence(panels)
    cross_train, cross_future = kp.combine(oofs), kp.combine(crosses)

    def build_frame(frame, site, cross, need_mlp=False):
        out = attach_temporal(kp.add_cross_site(frame, cross), t_panel)
        out = out.join(agg, on=['origin', 'valid_at'], how='left', validate='1:1') \
                 .join(dev[site], on=['origin', 'valid_at'], how='left', validate='1:1')
        if out.select(pl.any_horizontal(pl.col(AGGS + ['speed_dev']).is_null())).to_series().any():
            raise ValueError('Missing coherence features')
        out = out.with_columns(pl.lit(site).alias('site'))
        if need_mlp:
            out = out.with_columns(pl.lit(site).alias('farm'))
        return out.sort('origin', 'valid_at')

    models, validations, tests, mlp_validations = {}, [], [], []
    for site in range(1, 11):
        tr = parts[site]['train'].join(cross_train, on=['origin', 'valid_at'], how='inner', validate='1:1') \
            .sort('origin', 'valid_at')
        expected = pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv', try_parse_dates=True)
        if not tr.select('origin', 'valid_at').equals(expected):
            raise ValueError('Training keys differ')
        tr = build_frame(tr, site, cross_train)
        va = build_frame(parts[site]['validation'], site, cross_future, need_mlp=True)
        x, y = tr.select(FEATURES).to_numpy(), tr['actual'].to_numpy()
        xv, yv = va.select(FEATURES).to_numpy(), va['actual'].to_numpy()
        model = LGBMRegressor(**params)
        model.fit(x, y, eval_set=[(xv, yv)], callbacks=[early_stopping(30, verbose=False)])
        models[site] = model
        validations.append(va.select(KEYS).with_columns(pl.Series(
            'dispersion', np.clip(model.predict(xv), 0, 1))))
        mlp_v = np.clip(np.mean([m.predict(mlp_design(va, mlp_bundle['columns']))
                                 for m in mlp_bundle['models']], axis=0), 0, 1)
        mlp_validations.append(va.select(KEYS).with_columns(pl.Series('mlp', mlp_v)))
        te = build_frame(parts[site]['test'], site, cross_future)
        tests.append(te.select(KEYS).with_columns(pl.Series(
            'dispersion', np.clip(model.predict(te.select(FEATURES).to_numpy()), 0, 1))))
        print(f'Fitted zone {site} rounds={model.best_iteration_}', flush=True)
    joblib.dump(dict(models=models, columns=FEATURES, protocol=protocol), OUT / 'model.joblib')

    candidate_v = pl.concat(validations).sort(KEYS)
    mlp_v = pl.concat(mlp_validations).sort(KEYS)
    curve_v = pl.read_csv(PARENT / 'curve_residual_v1/validation_predictions.csv', try_parse_dates=True) \
        .select(['origin', 'valid_at', 'site', 'curve_blend'])
    w_m = json.loads((PARENT / 'mlp_seedavg_tuned_v1/weights.json').read_text())
    baseline_v = curve_v.join(mlp_v.select(['origin', 'valid_at', 'site', 'mlp']),
                              on=['origin', 'valid_at', 'site'], how='inner', validate='1:1')
    baseline_v = baseline_v.with_columns(
        (pl.col('curve_blend') * w_m['baseline'] + pl.col('mlp') * w_m['mlp_seedavg']).alias('baseline'))
    val = candidate_v.join(baseline_v.select(['origin', 'valid_at', 'site', 'baseline']),
                           on=['origin', 'valid_at', 'site'], how='inner', validate='1:1')
    weights = fit_weights(np.column_stack([val['baseline'].to_numpy(), val['dispersion'].to_numpy()]),
                          val['actual'].to_numpy())
    blended_v = np.clip(val['baseline'].to_numpy() * weights[0] + val['dispersion'].to_numpy() * weights[1], 0, 1)
    val_result = val.select(KEYS).with_columns(pl.Series('baseline', val['baseline']),
                                               pl.Series('dispersion', val['dispersion']),
                                               pl.Series('candidate', blended_v))
    val_scores = [dict(model=n, **metrics(val_result['actual'], val_result[n]))
                  for n in ('baseline', 'dispersion', 'candidate')]
    val_paired = paired_comparison(val_result, 'candidate')
    gate = val_scores[2]['RMSE'] < val_scores[0]['RMSE'] and val_paired['delta_RMSE_high'] < 0
    (OUT / 'validation_gate.json').write_text(json.dumps(dict(
        weights=dict(baseline=float(weights[0]), dispersion=float(weights[1])),
        scores=val_scores, paired=val_paired, passed=bool(gate)), indent=2))
    print(json.dumps(dict(weights=[float(weights[0]), float(weights[1])],
                          validation_scores=val_scores,
                          validation_paired={k: val_paired[k] for k in ('delta_RMSE', 'delta_RMSE_low', 'delta_RMSE_high')},
                          gate_passed=bool(gate)), indent=2), flush=True)

    if not gate:
        (OUT / 'decision.json').write_text(json.dumps(
            dict(accepted=False, reason='validation gate failed', weights=[float(w) for w in weights]), indent=2))
        print('Discarded at validation gate; test unopened.', flush=True)
        return

    previous = pl.read_csv(PARENT / 'mlp_seedavg_tuned_v1/predictions.csv', try_parse_dates=True)
    candidate = pl.concat(tests).sort(KEYS)
    if not candidate.select(KEYS).equals(previous.select(KEYS)):
        raise ValueError('Test keys differ')
    test = candidate.join(previous.select(KEYS + ['mlp_seedavg_blend']),
                          on=KEYS, how='inner', validate='1:1')
    final = np.clip(test['mlp_seedavg_blend'].to_numpy() * weights[0]
                    + test['dispersion'].to_numpy() * weights[1], 0, 1)
    result = test.select(KEYS).with_columns(
        pl.Series('baseline', test['mlp_seedavg_blend']),
        pl.Series('dispersion', test['dispersion']),
        pl.Series('dispersion_blend', final))
    scores = [dict(model=n, **metrics(result['actual'], result[n]))
              for n in ('baseline', 'dispersion', 'dispersion_blend')]
    paired = paired_comparison(result, 'dispersion_blend')
    accepted = scores[-1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    pl.DataFrame(scores).write_csv(OUT / 'metrics.csv')
    (OUT / 'paired.json').write_text(json.dumps(paired, indent=2))
    (OUT / 'decision.json').write_text(json.dumps(dict(accepted=accepted, scores=scores), indent=2))
    if accepted:
        result.write_csv(OUT / 'predictions.csv')
    print(json.dumps(dict(accepted=accepted, scores=scores, paired=paired), indent=2), flush=True)


if __name__ == '__main__':
    main()
