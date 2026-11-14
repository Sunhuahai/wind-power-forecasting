"""Segment 1: 2012 in-period OOF feasibility check for a meta-learner.

No validation or test labels are read. Every component is refitted on the
other 2012 time blocks and scored on the held-out block, so the meta-learner
sees honest in-period component predictions.
"""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor
from sklearn.isotonic import IsotonicRegression
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from windpower.features.build import build
from windpower.features.cross_nwp import spatial_panel, temporal_spatial_panel, attach_spatial, attach_temporal, COLUMNS, TEMPORAL_COLUMNS
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.models.validation_blend import fit_weights

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
OUT = PARENT / 'meta_oof_feasibility_v1'
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
COMPS = ['C', 'neighbors', 'full', 'spatial', 'temporal', 'shared', 'curve', 'mlp']
META = COMPS + ['lead', 'site', 'hour', 'speed100', 'speed_mean', 'speed_spread']
ROUNDS = dict(base=400, spatial=281, temporal=400, shared=709, curve=772)
FOLDS = 3


def main():
    OUT.mkdir(exist_ok=False)
    cfg = config()
    base_params = dict(cfg['lightgbm'], num_leaves=31, min_child_samples=80)

    stage1 = joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib')))['stage1']
    panels, parts, oofs, val_parts, test_parts = {}, {}, [], [], []
    for site in range(1, 11):
        panel = kp.nwp_panel(site)
        panels[site] = panel
        parts[site] = {k: kp.attach(v, panel) for k, v in partitions(build(site)).items()}
        oof = kp.stage1_oof(parts[site]['train'], panel, cfg['lightgbm'], site)
        oofs.append(oof)
        print(f'stage-one OOF zone {site}', flush=True)
    s_panel, t_panel = spatial_panel(panels), temporal_spatial_panel(panels)
    cross_train = kp.combine(oofs)
    cross_future = kp.combine([p.select('origin', 'valid_at').with_columns(pl.Series(
        f'stage1_{s}', np.clip(stage1[s].predict(p.select(kp.STAGE1).to_numpy()), 0, 1))) for s, p in panels.items()])

    rows = {}
    for site in range(1, 11):
        tr = parts[site]['train'].join(cross_train, on=['origin', 'valid_at'], how='inner', validate='1:1') \
            .sort('origin', 'valid_at')
        rows[site] = attach_temporal(tr, t_panel).with_columns(
            pl.lit(site).alias('farm'), pl.lit(site).alias('site'))
    all_origins = sorted({o for f in rows.values() for o in f['origin'].unique().to_list()})
    edges = np.linspace(0, len(all_origins), FOLDS + 1).astype(int)
    fold_sets = [set(all_origins[edges[i]:edges[i + 1]]) for i in range(FOLDS)]
    print(f'origins={len(all_origins)} folds={[len(f) for f in fold_sets]}', flush=True)

    F = dict(C=kp.C, neighbors=kp.C + kp.EXTRA, full=kp.STAGE2,
             spatial=kp.STAGE2 + COLUMNS, temporal=kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS,
             shared=kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS + ['farm'],
             curve=kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS + ['curve', 'farm'])
    keep = KEYS + ['speed100', 'hour'] + COMPS
    frames = []
    for fi, hold in enumerate(fold_sets):
        hold_list = list(hold)
        preds = {c: [] for c in COMPS}
        base_meta = []
        for site in range(1, 11):
            frame = rows[site]
            is_hold = frame['origin'].is_in(hold_list).to_numpy()
            tr, ho = frame.filter(~pl.Series(is_hold)), frame.filter(pl.Series(is_hold))
            for name in ('C', 'neighbors', 'full', 'spatial', 'temporal'):
                m = LGBMRegressor(**dict(base_params, n_estimators=ROUNDS.get(name, ROUNDS['base']),
                                         random_state=42))
                m.fit(tr.select(F[name]).to_numpy(), tr['actual'].to_numpy())
                preds[name].append(ho.select(KEYS).with_columns(pl.Series(
                    name, np.clip(m.predict(ho.select(F[name]).to_numpy()), 0, 1))))
            curve = IsotonicRegression(y_min=0, y_max=1, out_of_bounds='clip').fit(
                tr['speed100'].to_numpy(), tr['actual'].to_numpy())
            base_meta.append((tr, ho, curve))
            print(f'fold {fi} zone {site} base models done', flush=True)
        tr_all = pl.concat([b[0] for b in base_meta])
        ho_all = pl.concat([b[1] for b in base_meta])
        # shared pooled
        m = LGBMRegressor(**dict(base_params, n_estimators=ROUNDS['shared'], random_state=42))
        m.fit(tr_all.select(F['shared']).to_numpy(), tr_all['actual'].to_numpy(),
              categorical_feature=[len(F['shared']) - 1])
        shared_tr = np.clip(m.predict(tr_all.select(F['shared']).to_numpy()), 0, 1)
        preds['shared'].append(ho_all.select(KEYS).with_columns(pl.Series(
            'shared', np.clip(m.predict(ho_all.select(F['shared']).to_numpy()), 0, 1))))
        # curve residual
        tr_cv, ho_cv = [], []
        for site in range(1, 11):
            curve = next(b[2] for b in base_meta if b[0]['farm'][0] == site)
            tr_s = tr_all.filter(pl.col('farm') == site).with_columns(
                pl.Series('curve', curve.predict(tr_all.filter(pl.col('farm') == site)['speed100'].to_numpy())))
            ho_s = ho_all.filter(pl.col('farm') == site).with_columns(
                pl.Series('curve', curve.predict(ho_all.filter(pl.col('farm') == site)['speed100'].to_numpy())))
            tr_cv.append(tr_s)
            ho_cv.append(ho_s)
        tr_cv, ho_cv = pl.concat(tr_cv), pl.concat(ho_cv)
        m = LGBMRegressor(**dict(base_params, n_estimators=ROUNDS['curve'], random_state=42))
        m.fit(tr_cv.select(F['curve']).to_numpy(), (tr_cv['actual'] - tr_cv['curve']).to_numpy(),
              categorical_feature=[len(F['curve']) - 1])
        preds['curve'].append(ho_cv.select(KEYS).with_columns(pl.Series('curve', np.clip(
            ho_cv['curve'].to_numpy() + m.predict(ho_cv.select(F['curve']).to_numpy()), 0, 1))))
        # mlp pooled, single seed, fixed epochs
        def design(frame):
            farms = frame['farm'].to_numpy().astype(int)
            return np.column_stack([frame.select(F['temporal']).to_numpy(), np.eye(10)[farms - 1]])
        mlp = make_pipeline(StandardScaler(), MLPRegressor(
            hidden_layer_sizes=(128, 64, 32), alpha=0.1, batch_size=512, learning_rate_init=.001,
            max_iter=120, early_stopping=False, random_state=42))
        with threadpool_limits(limits=2):
            mlp.fit(design(tr_all), tr_all['actual'].to_numpy())
        preds['mlp'].append(ho_all.select(KEYS).with_columns(pl.Series(
            'mlp', np.clip(mlp.predict(design(ho_all)), 0, 1))))
        fold_frame = ho_all.select(['origin', 'valid_at', 'lead', 'actual', 'site', 'speed100'])
        print(f'fold {fi} base rows={fold_frame.height}', flush=True)
        for comp in COMPS:
            part = pl.concat(preds[comp])
            dedup = part.unique(subset=KEYS, keep='first')
            if dedup.height != part.height:
                print(f'fold {fi} {comp} duplicate keys {part.height - dedup.height}', flush=True)
            fold_frame = fold_frame.join(dedup, on=KEYS, how='inner', validate='1:1', suffix=f'_{comp}')
        fold_frame = fold_frame.with_columns(
            pl.col('valid_at').dt.hour().alias('hour'),
            pl.lit(fi).alias('fold'))
        frames.append(fold_frame)
        print(f'fold {fi} assembled rows={fold_frame.height}', flush=True)

    oof = pl.concat(frames).sort('origin', 'valid_at')
    speed = s_panel.select(['origin', 'valid_at'] + [f'nwp_{s}_speed100' for s in range(1, 11)])
    oof = oof.join(speed, on=['origin', 'valid_at'], how='inner', validate='m:1').with_columns(
        pl.concat_list([pl.col(f'nwp_{s}_speed100') for s in range(1, 11)]).list.mean().alias('speed_mean'),
        pl.concat_list([pl.col(f'nwp_{s}_speed100') for s in range(1, 11)]).list.std().alias('speed_spread'))
    oof.write_csv(OUT / 'oof_components.csv')

    # in-OOF comparison: fixed convex blend vs meta learner, fitted on the other folds
    results = []
    for fi in range(FOLDS):
        tr = oof.filter(pl.col('fold') != fi)
        ho = oof.filter(pl.col('fold') == fi)
        x_tr, y_tr = tr.select(META).to_numpy(), tr['actual'].to_numpy()
        x_ho, y_ho = ho.select(META).to_numpy(), ho['actual'].to_numpy()
        comps_tr = tr.select(COMPS).to_numpy()
        comps_ho = ho.select(COMPS).to_numpy()
        w = fit_weights(comps_tr, y_tr)
        blend = np.clip(comps_ho @ w, 0, 1)
        meta = LGBMRegressor(num_leaves=15, min_child_samples=200, learning_rate=0.05,
                             n_estimators=600, reg_lambda=1.0, random_state=42, n_jobs=2, verbosity=-1)
        meta.fit(x_tr, y_tr)
        pred = np.clip(meta.predict(x_ho), 0, 1)
        results.append(dict(fold=fi, n=len(y_ho),
                            rmse_blend=float(np.sqrt(np.mean((blend - y_ho) ** 2))),
                            rmse_meta=float(np.sqrt(np.mean((pred - y_ho) ** 2))),
                            weights=[float(v) for v in w]))
        print(f'fold {fi} blend={results[-1]["rmse_blend"]:.6f} meta={results[-1]["rmse_meta"]:.6f}', flush=True)
    total_n = sum(r['n'] for r in results)
    pooled_blend = float(np.sqrt(sum(r['n'] * r['rmse_blend'] ** 2 for r in results) / total_n))
    pooled_meta = float(np.sqrt(sum(r['n'] * r['rmse_meta'] ** 2 for r in results) / total_n))
    (OUT / 'oof_comparison.json').write_text(json.dumps(dict(
        folds=results, pooled=dict(rmse_blend=pooled_blend, rmse_meta=pooled_meta,
                                   delta=pooled_meta - pooled_blend)), indent=2))

    (OUT / 'summary.json').write_text(json.dumps(
        dict(oof_rows=oof.height, folds=FOLDS, rounds=ROUNDS,
             note='In-period OOF only; validation and test labels untouched'), indent=2))
    print(json.dumps(dict(pooled=dict(rmse_blend=pooled_blend, rmse_meta=pooled_meta),
                          delta=pooled_meta - pooled_blend), indent=2), flush=True)


if __name__ == '__main__':
    main()
