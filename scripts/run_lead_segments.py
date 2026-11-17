"""Per-lead-segment stage-two models (one change: whether to specialise by lead).

Pre-registration:
  change       : train one model per site per lead bucket (1-8, 9-16, 17-24)
                 instead of one model per site over all 24 leads
  frozen       : features, hyper-parameters, data split, early stopping rule,
                 blend procedure (convex weights on validation)
  data         : training 2012 OOF only; early stopping + blend weights 2013 H1
  acceptance   : validation gate (RMSE down and 7-day paired CI upper < 0),
                 then a single test pass with RMSE < 0.156009848
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
OUT = PARENT / 'lead_segments_v1'
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS
BUCKETS = [(1, 8), (9, 16), (17, 24)]


def mlp_design(frame, columns):
    farms = frame['farm'].to_numpy().astype(int)
    return np.column_stack([frame.select(columns).to_numpy(), np.eye(10)[farms - 1]])


def bucket_of(lead):
    for lo, hi in BUCKETS:
        if lo <= lead <= hi:
            return f'{lo}-{hi}'
    raise ValueError(lead)


def main():
    registry = json.loads(Path('docs/研究记录/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id'] != 'mlp_seedavg_tuned':
        raise ValueError('Baseline changed; refusing to run')
    OUT.mkdir(exist_ok=False)
    cfg = config()
    params = dict(cfg['lightgbm'], num_leaves=31, min_child_samples=80, n_estimators=600)
    protocol = dict(
        method='Per-lead-segment stage-two models (1-8 / 9-16 / 17-24) replacing one per-site model',
        features=FEATURES, buckets=BUCKETS, params=params,
        training_data='2012 OOF panel only', early_stopping='2013 H1 validation per segment',
        blend_weights='2013 H1 validation, convex between current best and new component',
        test_scoring='single pass, no tuning',
        acceptance='validation gate passes, then test RMSE below 0.156009848',
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
    cross_train, cross_future = kp.combine(oofs), kp.combine(crosses)

    models, val_frames, test_frames = {}, [], []
    segment_log = []
    for site in range(1, 11):
        tr = parts[site]['train'].join(cross_train, on=['origin', 'valid_at'], how='inner', validate='1:1') \
            .sort('origin', 'valid_at')
        expected = pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv', try_parse_dates=True)
        if not tr.select('origin', 'valid_at').equals(expected):
            raise ValueError('Training keys differ')
        tr = attach_temporal(tr, t_panel).with_columns(pl.lit(site).alias('site'))
        va = attach_temporal(kp.add_cross_site(parts[site]['validation'], cross_future), t_panel) \
            .with_columns(pl.lit(site).alias('site'), pl.lit(site).alias('farm'))
        te = attach_temporal(kp.add_cross_site(parts[site]['test'], cross_future), t_panel) \
            .with_columns(pl.lit(site).alias('site'), pl.lit(site).alias('farm'))
        mlp_v = np.clip(np.mean([m.predict(mlp_design(va, mlp_bundle['columns']))
                                 for m in mlp_bundle['models']], axis=0), 0, 1)
        va = va.with_columns(pl.Series('mlp', mlp_v))
        for lo, hi in BUCKETS:
            key = f'{lo}-{hi}'
            tr_b = tr.filter(pl.col('lead').is_between(lo, hi))
            va_b = va.filter(pl.col('lead').is_between(lo, hi))
            te_b = te.filter(pl.col('lead').is_between(lo, hi))
            model = LGBMRegressor(**params)
            model.fit(tr_b.select(FEATURES).to_numpy(), tr_b['actual'].to_numpy(),
                      eval_set=[(va_b.select(FEATURES).to_numpy(), va_b['actual'].to_numpy())],
                      callbacks=[early_stopping(30, verbose=False)])
            models[(site, key)] = model
            segment_log.append(dict(site=site, segment=key, train_rows=tr_b.height,
                                    validation_rows=va_b.height, rounds=int(model.best_iteration_)))
            val_frames.append(va_b.select(KEYS + ['mlp']).with_columns(pl.Series(
                'segmented', np.clip(model.predict(va_b.select(FEATURES).to_numpy()), 0, 1))))
            test_frames.append(te_b.select(KEYS).with_columns(pl.Series(
                'segmented', np.clip(model.predict(te_b.select(FEATURES).to_numpy()), 0, 1))))
        print(f'Fitted zone {site} three segments', flush=True)
    joblib.dump(dict(models={f'{s}_{k}': m for (s, k), m in models.items()},
                     columns=FEATURES, protocol=protocol), OUT / 'model.joblib')
    pl.DataFrame(segment_log).write_csv(OUT / 'segment_models.csv')

    cand_v = pl.concat(val_frames).sort(KEYS)
    curve_v = pl.read_csv(PARENT / 'curve_residual_v1/validation_predictions.csv', try_parse_dates=True) \
        .select(['origin', 'valid_at', 'site', 'curve_blend'])
    w_m = json.loads((PARENT / 'mlp_seedavg_tuned_v1/weights.json').read_text())
    base_v = curve_v.join(cand_v.select(['origin', 'valid_at', 'site', 'mlp']),
                          on=['origin', 'valid_at', 'site'], how='inner', validate='1:1')
    base_v = base_v.with_columns((pl.col('curve_blend') * w_m['baseline']
                                  + pl.col('mlp') * w_m['mlp_seedavg']).alias('baseline'))
    val = cand_v.join(base_v.select(['origin', 'valid_at', 'site', 'baseline']),
                      on=['origin', 'valid_at', 'site'], how='inner', validate='1:1')
    weights = fit_weights(np.column_stack([val['baseline'].to_numpy(), val['segmented'].to_numpy()]),
                          val['actual'].to_numpy())
    blended = np.clip(val['baseline'].to_numpy() * weights[0] + val['segmented'].to_numpy() * weights[1], 0, 1)
    val_out = val.select(KEYS).with_columns(pl.Series('baseline', val['baseline']),
                                            pl.Series('segmented', val['segmented']),
                                            pl.Series('candidate', blended))
    val_out = val_out.with_columns(pl.when(pl.col('lead') <= 8).then(pl.lit('1-8'))
                                   .when(pl.col('lead') <= 16).then(pl.lit('9-16'))
                                   .otherwise(pl.lit('17-24')).alias('segment'))
    by_segment = val_out.group_by('segment').agg(
        pl.len().alias('n'),
        ((pl.col('baseline') - pl.col('actual')) ** 2).mean().sqrt().alias('baseline_rmse'),
        ((pl.col('segmented') - pl.col('actual')) ** 2).mean().sqrt().alias('segmented_rmse'),
        ((pl.col('candidate') - pl.col('actual')) ** 2).mean().sqrt().alias('blended_rmse')).sort('segment')
    scores = [dict(model=n, **metrics(val_out['actual'], val_out[n]))
              for n in ('baseline', 'segmented', 'candidate')]
    val_paired = paired_comparison(val_out, 'candidate')
    gate = scores[2]['RMSE'] < scores[0]['RMSE'] and val_paired['delta_RMSE_high'] < 0
    gate_doc = dict(weights=dict(baseline=float(weights[0]), segmented=float(weights[1])),
                    scores=scores, paired=val_paired, passed=bool(gate),
                    by_segment=by_segment.to_dicts())
    (OUT / 'validation_gate.json').write_text(json.dumps(gate_doc, indent=2))
    print(json.dumps(gate_doc, indent=2), flush=True)
    if not gate:
        (OUT / 'decision.json').write_text(json.dumps(dict(accepted=False, reason='validation gate failed'), indent=2))
        print('Discarded at validation gate; test unopened.', flush=True)
        return

    previous = pl.read_csv(PARENT / 'mlp_seedavg_tuned_v1/predictions.csv', try_parse_dates=True)
    cand_t = pl.concat(test_frames).sort(KEYS)
    test = cand_t.join(previous.select(KEYS + ['mlp_seedavg_blend']), on=KEYS, how='inner', validate='1:1')
    if test.height != previous.height or test.height != 42432:
        raise ValueError(f'Test keys differ: {test.height}')
    final = np.clip(test['mlp_seedavg_blend'].to_numpy() * weights[0] + test['segmented'].to_numpy() * weights[1], 0, 1)
    result = test.select(KEYS).with_columns(pl.Series('baseline', test['mlp_seedavg_blend']),
                                            pl.Series('segmented', test['segmented']),
                                            pl.Series('segmented_blend', final))
    scores = [dict(model=n, **metrics(result['actual'], result[n])) for n in ('baseline', 'segmented', 'segmented_blend')]
    paired = paired_comparison(result, 'segmented_blend')
    accepted = scores[-1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    pl.DataFrame(scores).write_csv(OUT / 'metrics.csv')
    (OUT / 'paired.json').write_text(json.dumps(paired, indent=2))
    (OUT / 'decision.json').write_text(json.dumps(dict(accepted=accepted, scores=scores), indent=2))
    if accepted:
        result.write_csv(OUT / 'predictions.csv')
    print(json.dumps(dict(accepted=accepted, scores=scores, paired=paired), indent=2), flush=True)


if __name__ == '__main__':
    main()
