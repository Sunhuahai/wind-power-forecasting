"""Rolling in-period OOF residual features (target encoding of stage-one errors).

Hard constraints:
  * residual statistics use only labels strictly before the block being predicted
  * nothing from the validation or test periods enters the features; only 2012 in-period rolling
  * control: the same features rebuilt from a shuffled block order must perform clearly worse,
    otherwise the gain is leakage rather than time information

Test gate: validation 7-day block CI upper must be <= -0.0002 before the test set is opened.
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
OUT = PARENT / 'rolling_residual_features_v1'
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS
RESID = ['resid_mean_1', 'resid_abs_1', 'resid_std_1', 'resid_mean_2', 'resid_abs_2']
BLOCK_DAYS = 30
GATE = -0.0002


def lead_bucket(lead):
    return 'a' if lead <= 8 else ('b' if lead <= 16 else 'c')


def mlp_design(frame, columns):
    farms = frame['farm'].to_numpy().astype(int)
    return np.column_stack([frame.select(columns).to_numpy(), np.eye(10)[farms - 1]])


def rolling_features(oof, block_order=None):
    """Per site and lead bucket: stats over the two previous blocks only."""
    block_days = (oof['origin'] - oof['origin'].min()).dt.total_days() // BLOCK_DAYS
    frame = oof.with_columns(block_days.alias('block'))
    stats = frame.group_by(['site', 'block', 'bucket']).agg(
        pl.col('resid').mean().alias('resid_mean'),
        pl.col('resid').abs().mean().alias('resid_abs'),
        pl.col('resid').std().alias('resid_std'))
    blocks = sorted(frame['block'].unique().to_list())
    lookup = {(r['site'], r['block'], r['bucket']): (r['resid_mean'], r['resid_abs'], r['resid_std'])
              for r in stats.to_dicts()}
    order = {b: i for i, b in enumerate(blocks)} if block_order is None else block_order
    rows = []
    for (site, block, bucket), (m, a, s) in lookup.items():
        i = order[block]
        prev1 = next((b for b, j in order.items() if j == i - 1), None)
        prev2 = next((b for b, j in order.items() if j == i - 2), None)
        p1 = lookup.get((site, prev1, bucket), (0.0, 0.0, 0.0))
        p2 = lookup.get((site, prev2, bucket), (0.0, 0.0, 0.0))
        rows.append(dict(site=site, block=block, bucket=bucket,
                         resid_mean_1=p1[0], resid_abs_1=p1[1], resid_std_1=p1[2],
                         resid_mean_2=p2[0], resid_abs_2=p2[1]))
    return pl.DataFrame(rows), blocks


def main():
    registry = json.loads(Path('docs/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id'] != 'mlp_seedavg_tuned':
        raise ValueError('Baseline changed; refusing to run')
    OUT.mkdir(exist_ok=False)
    cfg = config()
    params = dict(cfg['lightgbm'], num_leaves=31, min_child_samples=80, n_estimators=600)
    protocol = dict(
        method='Rolling in-period OOF residual features (target encoding of stage-one errors)',
        features=FEATURES + RESID, block_days=BLOCK_DAYS, lags=2,
        training_data='2012 OOF panel only; residual stats from strictly earlier blocks',
        early_stopping='2013 H1 validation',
        blend_weights='2013 H1 validation, convex',
        validation_gate=f'CI upper <= {GATE} required before the test set is opened',
        control='shuffled block order must perform clearly worse',
        caveat='Exploratory historical test; NWP issue times unknown')
    (OUT / 'protocol.json').write_text(json.dumps(protocol, indent=2))

    stage1 = joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib')))['stage1']
    mlp_bundle = joblib.load(PARENT / 'mlp_seedavg_tuned_v1/model.joblib')
    panels, parts, oofs, resid_frames, crosses = {}, {}, [], [], []
    for site in range(1, 11):
        panel = kp.nwp_panel(site)
        panels[site] = panel
        parts[site] = {k: kp.attach(v, panel) for k, v in partitions(build(site)).items()}
        oof = kp.stage1_oof(parts[site]['train'], panel, cfg['lightgbm'], site)
        raw = oof
        oofs.append(raw.select(['origin', 'valid_at', f'stage1_{site}']))
        oof = raw.join(parts[site]['train'].select(['origin', 'valid_at', 'lead', 'actual']),
                       on=['origin', 'valid_at'], how='inner', validate='1:1').with_columns(
            (pl.col('actual') - pl.col(f'stage1_{site}')).alias('resid'), pl.lit(site).alias('site'))
        oof = oof.with_columns(pl.col('lead').cast(pl.Int64)).with_columns(
            pl.when(pl.col('lead') <= 8).then(pl.lit('a'))
            .when(pl.col('lead') <= 16).then(pl.lit('b')).otherwise(pl.lit('c')).alias('bucket'))
        resid_frames.append(oof.select(['origin', 'valid_at', 'lead', 'actual', 'resid', 'site', 'bucket']))
        crosses.append(panel.select('origin', 'valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()), 0, 1))))
        print(f'Prepared zone {site}', flush=True)
    t_panel = temporal_spatial_panel(panels)
    cross_train, cross_future = kp.combine(oofs), kp.combine(crosses)
    oof_all = pl.concat(resid_frames)
    real_feat, blocks = rolling_features(oof_all)
    rng = np.random.default_rng(42)
    shuffled_blocks = {b: i for i, b in enumerate(rng.permutation(blocks))}
    shuffle_feat, _ = rolling_features(oof_all, shuffled_blocks)
    last_block = max(blocks)
    frozen_feat = real_feat.filter(pl.col('block') == last_block)
    frozen_shuffle = shuffle_feat.filter(pl.col('block') == last_block)

    def attach(frame, site, cross, feat, block=None):
        out = attach_temporal(kp.add_cross_site(frame, cross), t_panel).with_columns(pl.lit(site).alias('site'))
        if block is None:
            out = out.with_columns(((pl.col('origin') - oof_all['origin'].min()).dt.total_days() // BLOCK_DAYS).alias('block'))
        else:
            out = out.with_columns(pl.lit(block).alias('block'))
        out = out.with_columns(
            pl.when(pl.col('lead') <= 8).then(pl.lit('a'))
            .when(pl.col('lead') <= 16).then(pl.lit('b')).otherwise(pl.lit('c')).alias('bucket'))
        return out.join(feat, on=['site', 'block', 'bucket'], how='left', validate='m:1').sort('origin', 'valid_at')

    val_frames, test_frames = [], []
    for site in range(1, 11):
        tr = parts[site]['train'].join(cross_train, on=['origin', 'valid_at'], how='inner', validate='1:1') \
            .sort('origin', 'valid_at')
        expected = pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv', try_parse_dates=True)
        if not tr.select('origin', 'valid_at').equals(expected):
            raise ValueError('Training keys differ')
        tr = attach(tr, site, cross_train, real_feat).filter(pl.col('resid_mean_1').is_not_null())
        va = attach(parts[site]['validation'], site, cross_future, frozen_feat, block=last_block).with_columns(pl.lit(site).alias('farm'))
        te = attach(parts[site]['test'], site, cross_future, frozen_feat, block=last_block)
        mlp_v = np.clip(np.mean([m.predict(mlp_design(va, mlp_bundle['columns']))
                                 for m in mlp_bundle['models']], axis=0), 0, 1)
        va = va.with_columns(pl.Series('mlp', mlp_v))
        model = LGBMRegressor(**params)
        model.fit(tr.select(FEATURES + RESID).to_numpy(), tr['actual'].to_numpy(),
                  eval_set=[(va.select(FEATURES + RESID).to_numpy(), va['actual'].to_numpy())],
                  callbacks=[early_stopping(30, verbose=False)])
        val_frames.append(va.select(KEYS + ['mlp']).with_columns(pl.Series(
            'resid_feat', np.clip(model.predict(va.select(FEATURES + RESID).to_numpy()), 0, 1))))
        test_frames.append(te.select(KEYS).with_columns(pl.Series(
            'resid_feat', np.clip(model.predict(te.select(FEATURES + RESID).to_numpy()), 0, 1))))
        # control: same pipeline with shuffled block order
        tr_s = attach(parts[site]['train'].join(cross_train, on=['origin', 'valid_at'], how='inner', validate='1:1')
                      .sort('origin', 'valid_at'), site, cross_train, shuffle_feat).filter(pl.col('resid_mean_1').is_not_null())
        va_s = attach(parts[site]['validation'], site, cross_future, frozen_shuffle, block=last_block).with_columns(pl.lit(site).alias('farm'))
        model_s = LGBMRegressor(**params)
        model_s.fit(tr_s.select(FEATURES + RESID).to_numpy(), tr_s['actual'].to_numpy(),
                    eval_set=[(va_s.select(FEATURES + RESID).to_numpy(), va_s['actual'].to_numpy())],
                    callbacks=[early_stopping(30, verbose=False)])
        val_frames[-1] = val_frames[-1].with_columns(pl.Series(
            'shuffled', np.clip(model_s.predict(va_s.select(FEATURES + RESID).to_numpy()), 0, 1)))
        print(f'Fitted zone {site} rounds={model.best_iteration_} control_rounds={model_s.best_iteration_}', flush=True)

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

    results = {}
    for name, col in (('resid_feat', 'resid_feat'), ('shuffled', 'shuffled')):
        w = fit_weights(np.column_stack([val['baseline'].to_numpy(), val[col].to_numpy()]), val['actual'].to_numpy())
        blended = np.clip(val['baseline'].to_numpy() * w[0] + val[col].to_numpy() * w[1], 0, 1)
        frame = val.select(KEYS).with_columns(pl.Series('baseline', val['baseline']),
                                             pl.Series('candidate', blended))
        scores = [dict(model=n, **metrics(frame['actual'], frame[n])) for n in ('baseline', 'candidate')]
        paired = paired_comparison(frame, 'candidate')
        results[name] = dict(weight=float(w[1]), scores=scores, paired=paired)
    gate = (results['resid_feat']['scores'][1]['RMSE'] < results['resid_feat']['scores'][0]['RMSE']
            and results['resid_feat']['paired']['delta_RMSE_high'] <= GATE
            and results['resid_feat']['scores'][1]['RMSE'] < results['shuffled']['scores'][1]['RMSE'])
    (OUT / 'validation_gate.json').write_text(json.dumps(dict(results=results, gate=GATE, passed=bool(gate)), indent=2))
    print(json.dumps(dict(real=results['resid_feat']['scores'][1]['RMSE'],
                          shuffled=results['shuffled']['scores'][1]['RMSE'],
                          baseline=results['resid_feat']['scores'][0]['RMSE'],
                          real_delta=results['resid_feat']['paired']['delta_RMSE'],
                          real_ci=[results['resid_feat']['paired']['delta_RMSE_low'],
                                   results['resid_feat']['paired']['delta_RMSE_high']],
                          shuffled_delta=results['shuffled']['paired']['delta_RMSE'],
                          gate_passed=bool(gate)), indent=2), flush=True)
    if not gate:
        (OUT / 'decision.json').write_text(json.dumps(dict(accepted=False, reason='validation gate failed'), indent=2))
        print('Discarded at validation gate; test unopened.', flush=True)
        return

    previous = pl.read_csv(PARENT / 'mlp_seedavg_tuned_v1/predictions.csv', try_parse_dates=True)
    cand_t = pl.concat(test_frames).sort(KEYS)
    test = cand_t.join(previous.select(KEYS + ['mlp_seedavg_blend']), on=KEYS, how='inner', validate='1:1')
    if test.height != 42432:
        raise ValueError(f'Test keys differ: {test.height}')
    w = results['resid_feat']
    final = np.clip(test['mlp_seedavg_blend'].to_numpy() * (1 - w['weight'])
                    + test['resid_feat'].to_numpy() * w['weight'], 0, 1)
    result = test.select(KEYS).with_columns(pl.Series('baseline', test['mlp_seedavg_blend']),
                                            pl.Series('resid_feat_blend', final))
    scores = [dict(model=n, **metrics(result['actual'], result[n])) for n in ('baseline', 'resid_feat_blend')]
    paired = paired_comparison(result, 'resid_feat_blend')
    accepted = scores[-1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    pl.DataFrame(scores).write_csv(OUT / 'metrics.csv')
    (OUT / 'paired.json').write_text(json.dumps(paired, indent=2))
    (OUT / 'decision.json').write_text(json.dumps(dict(accepted=accepted, scores=scores), indent=2))
    if accepted:
        result.write_csv(OUT / 'predictions.csv')
    print(json.dumps(dict(accepted=accepted, scores=scores, paired=paired), indent=2), flush=True)


if __name__ == '__main__':
    main()
