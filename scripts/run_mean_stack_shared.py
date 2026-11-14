"""Pooled branch with both mean and median first-stage OOF representations."""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor,early_stopping

from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel,attach_temporal,COLUMNS,TEMPORAL_COLUMNS
from windpower.models.train import config,partitions
from windpower.models import kpower_reproduction as kp
from windpower.models.mean_stage1 import mean_oof,estimator as mean_estimator,COLUMNS as MEANS
from windpower.models.validation_blend import fit_weights
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID='ablation_20261114_v1'
PARENT=Path('artifacts/kpower_ablation')/ID
BASE=PARENT/'mlp_shared_v1'
OUT=PARENT/'mean_stack_shared_v1'
FEATURES=kp.STAGE2+COLUMNS+TEMPORAL_COLUMNS+MEANS+['farm']
KEYS=['origin','valid_at','lead','actual','site']


def main():
    OUT.mkdir(exist_ok=False)
    cfg=config()
    params=dict(cfg['lightgbm'],num_leaves=31,min_child_samples=80,n_estimators=800)
    protocol=dict(params=params,features=FEATURES,categorical_feature='farm',baseline=str(BASE),
        method='Add mean first-stage predictions alongside frozen median representations; mean OOF labels strictly before each block origin',
        selection='Validation early stopping and global convex MSE blend; freeze before test scoring',
        caveat='Exploratory historical test, validation reused for early stopping')
    (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    stage1=joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib')))['stage1']
    panels,parts,oofs,crosses,mean_models={},{},[],[],{}
    for site in range(1,11):
        panel=kp.nwp_panel(site); panels[site]=panel
        parts[site]={k:kp.attach(v,panel) for k,v in partitions(build(site)).items()}
        oofs.append(kp.stage1_oof(parts[site]['train'],panel,cfg['lightgbm'],site))
        oofs.append(mean_oof(parts[site]['train'],panel,cfg['lightgbm'],site))
        mean_model=mean_estimator(cfg['lightgbm'])
        mean_model.fit(parts[site]['train'].select(kp.STAGE1).to_numpy(),parts[site]['train']['actual'].to_numpy(),eval_set=[(parts[site]['validation'].select(kp.STAGE1).to_numpy(),parts[site]['validation']['actual'].to_numpy())],callbacks=[early_stopping(30,verbose=False)])
        mean_models[site]=mean_model
        crosses.append(panel.select('origin','valid_at').with_columns(pl.Series(f'stage1_mean_{site}',np.clip(mean_model.predict(panel.select(kp.STAGE1).to_numpy()),0,1))))
        crosses.append(panel.select('origin','valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()),0,1))))
        print(f'Prepared zone {site}',flush=True)
    spatial=temporal_spatial_panel(panels)
    cross_train,cross_future=kp.combine(oofs),kp.combine(crosses)
    def add_cross(frame):
        out=kp.add_cross_site(frame,cross_future)
        if not np.isfinite(out.select(MEANS).to_numpy()).all():
            raise ValueError('Missing first-stage means')
        return out
    joblib.dump(mean_models,OUT/'mean_stage1.joblib')
    trains,validations=[],[]
    for site in range(1,11):
        tr=parts[site]['train'].join(cross_train,on=['origin','valid_at'],how='inner',validate='1:1').sort('origin','valid_at')
        expected=pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv',try_parse_dates=True)
        if not tr.select('origin','valid_at').equals(expected):raise ValueError('Training keys differ')
        trains.append(attach_temporal(tr,spatial).with_columns(pl.lit(site).alias('farm')))
        validations.append(attach_temporal(add_cross(parts[site]['validation']),spatial)
            .with_columns(pl.lit(site).alias('farm'),pl.lit(site).alias('site')))
    tr,va=pl.concat(trains),pl.concat(validations)
    model=LGBMRegressor(**params)
    model.fit(tr.select(FEATURES).to_numpy(),tr['actual'].to_numpy(),
        categorical_feature=[len(FEATURES)-1],eval_set=[(va.select(FEATURES).to_numpy(),va['actual'].to_numpy())],
        callbacks=[early_stopping(30,verbose=False)])
    joblib.dump(dict(model=model,mean_stage1=mean_models,median_stage1=stage1,columns=FEATURES,protocol=protocol),OUT/'model.joblib')
    candidate=np.clip(model.predict(va.select(FEATURES).to_numpy()),0,1)
    old=pl.read_csv(BASE/'validation_predictions.csv',try_parse_dates=True)
    if not va.select(KEYS).equals(old.select(KEYS)):raise ValueError('Validation keys/labels differ')
    baseline_v=old['mlp_blend'].to_numpy()
    weights=fit_weights(np.column_stack([baseline_v,candidate]),va['actual'].to_numpy())
    (OUT/'weights.json').write_text(json.dumps(dict(baseline=float(weights[0]),mean_stack=float(weights[1]),
        train_samples=tr.height,validation_samples=va.height,best_iteration=model.best_iteration_),indent=2))
    if weights[1] < 1e-8:
        (OUT/'decision.json').write_text(json.dumps(dict(accepted=False,reason='Validation chose zero candidate weight'),indent=2))
        print('Discarded at validation gate; test unopened.',flush=True)
        return
    # All model rounds and weights fixed before test inference.
    tests=[]
    for site in range(1,11):
        tests.append(attach_temporal(add_cross(parts[site]['test']),spatial)
            .with_columns(pl.lit(site).alias('farm'),pl.lit(site).alias('site')))
    te=pl.concat(tests)
    previous=pl.read_csv(BASE/'predictions.csv',try_parse_dates=True)
    if not te.select(KEYS).equals(previous.select(KEYS)):raise ValueError('Test keys/labels differ')
    test=te.select(KEYS).with_columns(pl.Series('baseline',previous['mlp_blend']),
        pl.Series('mean_stack',np.clip(model.predict(te.select(FEATURES).to_numpy()),0,1)))
    test=test.with_columns((pl.col('baseline')*weights[0]+pl.col('mean_stack')*weights[1]).alias('mean_stack_blend'))
    scores=[dict(model=n,**metrics(test['actual'],test[n])) for n in ['baseline','mean_stack','mean_stack_blend']]
    paired=paired_comparison(test,'mean_stack_blend')
    accepted=scores[-1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:
        test.write_csv(OUT/'predictions.csv')
        va.select(KEYS).with_columns(pl.Series('baseline',baseline_v),pl.Series('mean_stack',candidate),
            pl.Series('mean_stack_blend',baseline_v*weights[0]+candidate*weights[1])).write_csv(OUT/'validation_predictions.csv')
    print(json.dumps(dict(accepted=accepted,weights=weights.tolist(),scores=scores,paired=paired),indent=2),flush=True)


if __name__=='__main__':main()
