"""Temporal spatial NWP stage-two model, blended using validation MSE only."""
import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor, early_stopping

from run_validation_blend import load, NAMES
from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel, attach_temporal, COLUMNS, TEMPORAL_COLUMNS
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.models.validation_blend import fit_weights
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
OUT = PARENT / 'temporal_nwp_v1'
FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume-inference', action='store_true')
    resume = parser.parse_args().resume_inference
    OUT.mkdir(exist_ok=resume)
    cfg = config()
    seeds = [42]
    protocol = dict(seeds=seeds, added_features=COLUMNS+TEMPORAL_COLUMNS, baseline='spatial_nwp_v1/spatial_blend', origin_join='exact origin and valid_at',
                    base_config=cfg, validation_only_blend=True,
                    caveat='Fixed historical test; base models also use validation early stopping')
    if resume:
        if json.loads((OUT/'protocol.json').read_text()) != protocol:
            raise ValueError('Cannot resume changed protocol')
    else:
        (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    bundle_path = next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib'))
    stage1 = joblib.load(bundle_path)['stage1']
    parts, panels, oofs, cross_frames = {}, {}, [], []
    for site in range(1,11):
        panel = kp.nwp_panel(site)
        panels[site] = panel
        parts[site] = {k:kp.attach(v,panel) for k,v in partitions(build(site)).items()}
        if not resume:
            oofs.append(kp.stage1_oof(parts[site]['train'],panel,cfg['lightgbm'],site))
        cross_frames.append(panel.select('origin','valid_at').with_columns(
            pl.Series(f'stage1_{site}',np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()),0,1))))
        print(f'Prepared zone {site}',flush=True)
    cross_train = None if resume else kp.combine(oofs)
    cross_future = kp.combine(cross_frames)
    spatial = temporal_spatial_panel(panels)
    models, validations, prior_validations = {}, [], []
    for site in range(1,11):
        va = attach_temporal(kp.add_cross_site(parts[site]['validation'],cross_future), spatial)
        xv,yv=va.select(FEATURES).to_numpy(),va['actual'].to_numpy()
        if resume:
            saved=joblib.load(OUT/f'site_{site}.joblib')
            if saved['columns']!=FEATURES or saved['protocol']!=protocol:
                raise ValueError('Saved model protocol differs')
            models[site]=saved['models']
        else:
            tr = parts[site]['train'].join(cross_train,on=['origin','valid_at'],how='inner',validate='1:1').sort('origin','valid_at')
            expected = pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv',try_parse_dates=True)
            if not tr.select('origin','valid_at').equals(expected):
                raise ValueError('Training keys differ from frozen full model')
            tr = attach_temporal(tr, spatial)
            x,y=tr.select(FEATURES).to_numpy(),tr['actual'].to_numpy()
            models[site]=[]
            for seed in seeds:
                params = dict(cfg['lightgbm'],random_state=seed)
                model=LGBMRegressor(**params)
                model.fit(x,y,eval_set=[(xv,yv)],callbacks=[early_stopping(30,verbose=False)])
                models[site].append(model)
            joblib.dump(dict(models=models[site],columns=FEATURES,protocol=protocol),OUT/f'site_{site}.joblib')
        validations.append(va.select('origin','valid_at','lead','actual').with_columns(
            pl.lit(site).alias('site'),
            pl.Series('temporal',np.mean([np.clip(m.predict(xv),0,1) for m in models[site]],axis=0))))
        prior = joblib.load(PARENT/'spatial_nwp_v1'/f'site_{site}.joblib')
        prior_validations.append(np.mean([np.clip(m.predict(va.select(prior['columns']).to_numpy()),0,1) for m in prior['models']],axis=0))
        print(f'Fitted zone {site}',flush=True)
    candidate=pl.concat(validations)
    folders={n:Path(f'artifacts/kpower_reproduction/{ID}_{n}') for n in NAMES}
    validation=load(folders,'validation_predictions.csv')
    if not candidate.select('origin','valid_at','lead','actual','site').equals(validation.select('origin','valid_at','lead','actual','site')):
        raise ValueError('Validation rows differ')
    old_weights=json.loads((PARENT/'validation_blend/weights.json').read_text())['weights']
    baseline_v=validation.select(NAMES).to_numpy()@np.array([old_weights[n] for n in NAMES])
    prior_weights=json.loads((PARENT/'spatial_nwp_v1/weights.json').read_text())
    baseline_v=baseline_v*prior_weights['baseline']+np.concatenate(prior_validations)*prior_weights['spatial']
    weights=fit_weights(np.column_stack([baseline_v,candidate['temporal'].to_numpy()]),candidate['actual'].to_numpy())
    (OUT/'weights.json').write_text(json.dumps(dict(baseline=float(weights[0]),temporal=float(weights[1]),
        validation_samples=validation.height,validation_MSE=float(np.mean((baseline_v*weights[0]+candidate['temporal'].to_numpy()*weights[1]-candidate['actual'].to_numpy())**2))),indent=2))
    # Test predictions begin only after model rounds and weights have been frozen.
    tests=[]
    for site in range(1,11):
        te=attach_temporal(kp.add_cross_site(parts[site]['test'],cross_future), spatial)
        xt=te.select(FEATURES).to_numpy()
        tests.append(te.select('origin','valid_at','lead','actual').with_columns(
            pl.lit(site).alias('site'),pl.Series('temporal',np.mean([np.clip(m.predict(xt),0,1) for m in models[site]],axis=0))))
    test=pl.concat(tests)
    previous=pl.read_csv(PARENT/'spatial_nwp_v1/predictions.csv',try_parse_dates=True)
    if not test.select('origin','valid_at','lead','actual','site').equals(previous.select('origin','valid_at','lead','actual','site')):
        raise ValueError('Test samples differ from previous best')
    test=test.with_columns(pl.Series('baseline',previous['spatial_blend']))
    test=test.with_columns((pl.col('baseline')*weights[0]+pl.col('temporal')*weights[1]).alias('temporal_blend'))
    scores=[dict(model=n,**metrics(test['actual'],test[n])) for n in ['baseline','temporal','temporal_blend']]
    paired=paired_comparison(test,'temporal_blend')
    accepted=scores[-1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:
        test.write_csv(OUT/'predictions.csv')
        candidate.with_columns(pl.Series('baseline',baseline_v),pl.Series('temporal_blend',baseline_v*weights[0]+candidate['temporal'].to_numpy()*weights[1])).write_csv(OUT/'validation_predictions.csv')
    print(json.dumps(dict(accepted=accepted,weights=weights.tolist(),scores=scores,paired=paired),indent=2),flush=True)


if __name__=='__main__':
    main()
