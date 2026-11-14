"""Fixed pooled HistGradientBoosting with explicit external validation and convex blend."""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits

from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel,attach_temporal,COLUMNS,TEMPORAL_COLUMNS
from windpower.models.train import config,partitions
from windpower.models import kpower_reproduction as kp
from windpower.models.validation_blend import fit_weights
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID='ablation_20261114_v1'
PARENT=Path('artifacts/kpower_ablation')/ID
BASE=PARENT/'mlp_shared_v1'
OUT=PARENT/'hist_gradient_v1'
FEATURES=kp.STAGE2+COLUMNS+TEMPORAL_COLUMNS+['farm']
KEYS=['origin','valid_at','lead','actual','site']


def main():
    OUT.mkdir(exist_ok=False)
    cfg=config()
    params=dict(loss='squared_error',learning_rate=.05,max_iter=800,max_leaf_nodes=31,min_samples_leaf=80,l2_regularization=5.,max_bins=255,categorical_features=[False]*(len(FEATURES)-1)+[True],early_stopping=True,validation_fraction=None,n_iter_no_change=30,tol=1e-7,random_state=42)
    protocol=dict(params=params,features=FEATURES,categorical_feature='farm',baseline=str(BASE),
        method='Pooled HistGradientBoosting on original honest OOF periods; fixed hyperparameters, no search',
        selection='Validation early stopping and global convex MSE blend; freeze before test scoring',
        caveat='Exploratory historical test, validation reused for early stopping')
    (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    stage1=joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib')))['stage1']
    panels,parts,oofs,crosses={},{},[],[]
    for site in range(1,11):
        panel=kp.nwp_panel(site); panels[site]=panel
        parts[site]={k:kp.attach(v,panel) for k,v in partitions(build(site)).items()}
        oofs.append(kp.stage1_oof(parts[site]['train'],panel,cfg['lightgbm'],site))
        crosses.append(panel.select('origin','valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()),0,1))))
        print(f'Prepared zone {site}',flush=True)
    spatial=temporal_spatial_panel(panels)
    cross_train,cross_future=kp.combine(oofs),kp.combine(crosses)
    trains,validations=[],[]
    for site in range(1,11):
        tr=parts[site]['train'].join(cross_train,on=['origin','valid_at'],how='inner',validate='1:1').sort('origin','valid_at')
        expected=pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv',try_parse_dates=True)
        if not tr.select('origin','valid_at').equals(expected):raise ValueError('Training keys differ')
        trains.append(attach_temporal(tr,spatial).with_columns(pl.lit(site).alias('farm')))
        validations.append(attach_temporal(kp.add_cross_site(parts[site]['validation'],cross_future),spatial)
            .with_columns(pl.lit(site).alias('farm'),pl.lit(site).alias('site')))
    tr,va=pl.concat(trains),pl.concat(validations)
    model=HistGradientBoostingRegressor(**params)
    print(f'Training HistGradientBoosting: {tr.height} rows',flush=True)
    with threadpool_limits(limits=2):
        model.fit(tr.select(FEATURES).to_numpy(),tr['actual'].to_numpy(),X_val=va.select(FEATURES).to_numpy(),y_val=va['actual'].to_numpy())
    joblib.dump(dict(model=model,columns=FEATURES,protocol=protocol),OUT/'model.joblib')
    candidate=np.clip(model.predict(va.select(FEATURES).to_numpy()),0,1)
    old=pl.read_csv(BASE/'validation_predictions.csv',try_parse_dates=True)
    if not va.select(KEYS).equals(old.select(KEYS)):raise ValueError('Validation keys/labels differ')
    baseline_v=old['mlp_blend'].to_numpy()
    weights=fit_weights(np.column_stack([baseline_v,candidate]),va['actual'].to_numpy())
    (OUT/'weights.json').write_text(json.dumps(dict(baseline=float(weights[0]),hist=float(weights[1]),
        train_samples=tr.height,validation_samples=va.height,iterations=int(model.n_iter_)),indent=2))
    if weights[1] < 1e-8:
        (OUT/'decision.json').write_text(json.dumps(dict(accepted=False,reason='Validation chose zero candidate weight'),indent=2))
        print('Discarded at validation gate; test unopened.',flush=True)
        return
    # All model rounds and weights fixed before test inference.
    tests=[]
    for site in range(1,11):
        tests.append(attach_temporal(kp.add_cross_site(parts[site]['test'],cross_future),spatial)
            .with_columns(pl.lit(site).alias('farm'),pl.lit(site).alias('site')))
    te=pl.concat(tests)
    previous=pl.read_csv(BASE/'predictions.csv',try_parse_dates=True)
    if not te.select(KEYS).equals(previous.select(KEYS)):raise ValueError('Test keys/labels differ')
    test=te.select(KEYS).with_columns(pl.Series('baseline',previous['mlp_blend']),
        pl.Series('hist',np.clip(model.predict(te.select(FEATURES).to_numpy()),0,1)))
    test=test.with_columns((pl.col('baseline')*weights[0]+pl.col('hist')*weights[1]).alias('hist_blend'))
    scores=[dict(model=n,**metrics(test['actual'],test[n])) for n in ['baseline','hist','hist_blend']]
    paired=paired_comparison(test,'hist_blend')
    accepted=scores[-1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:
        test.write_csv(OUT/'predictions.csv')
        va.select(KEYS).with_columns(pl.Series('baseline',baseline_v),pl.Series('hist',candidate),
            pl.Series('hist_blend',baseline_v*weights[0]+candidate*weights[1])).write_csv(OUT/'validation_predictions.csv')
    print(json.dumps(dict(accepted=accepted,weights=weights.tolist(),scores=scores,paired=paired),indent=2),flush=True)


if __name__=='__main__':main()
