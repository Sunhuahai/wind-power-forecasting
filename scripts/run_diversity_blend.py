"""Fixed bagged stage-two ensemble, blended using validation MSE only."""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor, early_stopping

from run_validation_blend import load, NAMES
from windpower.features.build import build
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.models.validation_blend import fit_weights
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
OUT = PARENT / 'diversity_v1'


def main():
    OUT.mkdir(exist_ok=False)
    cfg = config()
    seeds = [17, 73, 121]
    protocol = dict(seeds=seeds, subsample=.85, subsample_freq=1, colsample_bytree=.9,
                    base_config=cfg, validation_only_blend=True,
                    caveat='Fixed historical test; base models also use validation early stopping')
    (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    bundle_path = next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib'))
    stage1 = joblib.load(bundle_path)['stage1']
    parts, oofs, cross_frames = {}, [], []
    for site in range(1,11):
        panel = kp.nwp_panel(site)
        parts[site] = {k:kp.attach(v,panel) for k,v in partitions(build(site)).items()}
        oofs.append(kp.stage1_oof(parts[site]['train'],panel,cfg['lightgbm'],site))
        cross_frames.append(panel.select('origin','valid_at').with_columns(
            pl.Series(f'stage1_{site}',np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()),0,1))))
        print(f'Prepared zone {site}',flush=True)
    cross_train, cross_future = kp.combine(oofs), kp.combine(cross_frames)
    models, validations = {}, []
    for site in range(1,11):
        tr = parts[site]['train'].join(cross_train,on=['origin','valid_at'],how='inner',validate='1:1').sort('origin','valid_at')
        expected = pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv',try_parse_dates=True)
        if not tr.select('origin','valid_at').equals(expected):
            raise ValueError('Training keys differ from frozen full model')
        va = kp.add_cross_site(parts[site]['validation'],cross_future)
        x,y=tr.select(kp.STAGE2).to_numpy(),tr['actual'].to_numpy()
        xv,yv=va.select(kp.STAGE2).to_numpy(),va['actual'].to_numpy()
        models[site]=[]
        for seed in seeds:
            params = dict(cfg['lightgbm'],random_state=seed,subsample=.85,subsample_freq=1,colsample_bytree=.9)
            model=LGBMRegressor(**params)
            model.fit(x,y,eval_set=[(xv,yv)],callbacks=[early_stopping(30,verbose=False)])
            models[site].append(model)
        joblib.dump(dict(models=models[site],columns=kp.STAGE2,protocol=protocol),OUT/f'site_{site}.joblib')
        validations.append(va.select('origin','valid_at','lead','actual').with_columns(
            pl.lit(site).alias('site'),
            pl.Series('diversity',np.mean([np.clip(m.predict(xv),0,1) for m in models[site]],axis=0))))
        print(f'Fitted zone {site}',flush=True)
    candidate=pl.concat(validations)
    folders={n:Path(f'artifacts/kpower_reproduction/{ID}_{n}') for n in NAMES}
    validation=load(folders,'validation_predictions.csv')
    if not candidate.select('origin','valid_at','lead','actual','site').equals(validation.select('origin','valid_at','lead','actual','site')):
        raise ValueError('Validation rows differ')
    old_weights=json.loads((PARENT/'validation_blend/weights.json').read_text())['weights']
    baseline_v=validation.select(NAMES).to_numpy()@np.array([old_weights[n] for n in NAMES])
    weights=fit_weights(np.column_stack([baseline_v,candidate['diversity'].to_numpy()]),candidate['actual'].to_numpy())
    (OUT/'weights.json').write_text(json.dumps(dict(baseline=float(weights[0]),diversity=float(weights[1]),
        validation_samples=validation.height,validation_MSE=float(np.mean((baseline_v*weights[0]+candidate['diversity'].to_numpy()*weights[1]-candidate['actual'].to_numpy())**2))),indent=2))
    # Test predictions begin only after model rounds and weights have been frozen.
    tests=[]
    for site in range(1,11):
        te=kp.add_cross_site(parts[site]['test'],cross_future)
        xt=te.select(kp.STAGE2).to_numpy()
        tests.append(te.select('origin','valid_at','lead','actual').with_columns(
            pl.lit(site).alias('site'),pl.Series('diversity',np.mean([np.clip(m.predict(xt),0,1) for m in models[site]],axis=0))))
    test=pl.concat(tests)
    previous=pl.read_csv(PARENT/'validation_blend/predictions.csv',try_parse_dates=True)
    if not test.select('origin','valid_at','lead','actual','site').equals(previous.select('origin','valid_at','lead','actual','site')):
        raise ValueError('Test samples differ from previous best')
    test=test.with_columns(pl.Series('baseline',previous['blend']))
    test=test.with_columns((pl.col('baseline')*weights[0]+pl.col('diversity')*weights[1]).alias('diversity_blend'))
    scores=[dict(model=n,**metrics(test['actual'],test[n])) for n in ['baseline','diversity','diversity_blend']]
    paired=paired_comparison(test,'diversity_blend')
    accepted=scores[-1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:test.write_csv(OUT/'predictions.csv')
    print(json.dumps(dict(accepted=accepted,weights=weights.tolist(),scores=scores,paired=paired),indent=2),flush=True)


if __name__=='__main__':
    main()
