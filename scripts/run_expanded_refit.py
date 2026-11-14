"""Frozen-round final refit on train+validation with regenerated honest temporal OOF."""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl

from lightgbm import LGBMRegressor
from windpower.features.build import build
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID='ablation_20261114_v1'
PARENT=Path('artifacts/kpower_ablation')/ID
OUT=PARENT/'expanded_refit_v1'
NAMES=['C','neighbors','full']


def main():
    OUT.mkdir(exist_ok=False)
    cfg=config()
    baseline_weights=json.loads((PARENT/'validation_blend/weights.json').read_text())['weights']
    protocol=dict(config=cfg,final_training='original train + validation; test labels excluded',
        stage1='fixed original selected rounds, refit train+validation',
        stage2='fixed original selected rounds, refit honest OOF train+validation',
        stage1_oof=dict(min_days=kp.MIN_TRAIN_DAYS,block_days=kp.OOF_BLOCK_DAYS,trees=kp.OOF_TREES),
        weights=baseline_weights,selection='All rounds and weights frozen before expanded fitting',
        caveat='Exploratory historical test; no new validation after final refit')
    (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    parts,extended,oofs,crosses,stage1={},{},[],[],{}
    for site in range(1,11):
        panel=kp.nwp_panel(site)
        parts[site]={k:kp.attach(v,panel) for k,v in partitions(build(site)).items()}
        extended[site]=pl.concat([parts[site]['train'],parts[site]['validation']]).sort('origin','valid_at')
        if extended[site]['valid_at'].max()>=parts[site]['test']['valid_at'].min():
            raise ValueError('Final refit labels overlap test')
        original=joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_{site}').glob('*.joblib')))
        selected=original['stage1'][site].best_iteration_
        if selected<1:raise ValueError('Missing selected stage-one rounds')
        stage1[site]=kp.qmodel(cfg['lightgbm'],.5,selected)
        stage1[site].fit(extended[site].select(kp.STAGE1).to_numpy(),extended[site]['actual'].to_numpy())
        oofs.append(kp.stage1_oof(extended[site],panel,cfg['lightgbm'],site))
        crosses.append(panel.select('origin','valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()),0,1))))
        print(f'Expanded stage one zone {site}',flush=True)
    cross_train,cross_test=kp.combine(oofs),kp.combine(crosses)
    models,training_info={},[]
    for site in range(1,11):
        tr=extended[site].join(cross_train,on=['origin','valid_at'],how='inner',validate='1:1').sort('origin','valid_at')
        models[site]={}
        for name in NAMES:
            old=joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_{name}/site_{site}').glob('*.joblib')))
            rounds=old['point'].best_iteration_
            if rounds<1:raise ValueError('Missing selected stage-two rounds')
            columns=kp.stage2_columns(name)
            model=LGBMRegressor(**dict(cfg['lightgbm'],n_estimators=rounds))
            model.fit(tr.select(columns).to_numpy(),tr['actual'].to_numpy())
            models[site][name]=model
            training_info.append(dict(site=site,model=name,rounds=rounds,n=tr.height,
                first=str(tr['valid_at'].min()),last=str(tr['valid_at'].max())))
        joblib.dump(dict(stage2=models[site],stage1=stage1,weights=baseline_weights,protocol=protocol),OUT/f'site_{site}.joblib')
        print(f'Expanded stage two zone {site}: {tr.height} hours',flush=True)
    (OUT/'training.json').write_text(json.dumps(training_info,indent=2))
    tests=[]
    for site in range(1,11):
        te=kp.add_cross_site(parts[site]['test'],cross_test)
        result=te.select('origin','valid_at','lead','actual').with_columns(pl.lit(site).alias('site'))
        for name in NAMES:
            result=result.with_columns(pl.Series(name,np.clip(models[site][name].predict(te.select(kp.stage2_columns(name)).to_numpy()),0,1)))
        tests.append(result)
    test=pl.concat(tests)
    previous=pl.read_csv(PARENT/'validation_blend/predictions.csv',try_parse_dates=True)
    if not test.select('origin','valid_at','lead','actual','site').equals(previous.select('origin','valid_at','lead','actual','site')):
        raise ValueError('Test samples differ')
    test=test.with_columns(pl.Series('baseline',previous['blend']),
        pl.Series('expanded_refit',test.select(NAMES).to_numpy()@np.array([baseline_weights[n] for n in NAMES])))
    scores=[dict(model=n,**metrics(test['actual'],test[n])) for n in ['baseline','expanded_refit']]
    paired=paired_comparison(test,'expanded_refit')
    accepted=scores[1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:test.write_csv(OUT/'predictions.csv')
    print(json.dumps(dict(accepted=accepted,scores=scores,paired=paired),indent=2),flush=True)


if __name__=='__main__':
    main()
