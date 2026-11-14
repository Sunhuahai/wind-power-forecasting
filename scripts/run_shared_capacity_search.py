"""Predeclared validation-only capacity search for accepted pooled architecture."""
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
from windpower.models.validation_blend import fit_weights
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID='ablation_20261114_v1'
PARENT=Path('artifacts/kpower_ablation')/ID
BASE=PARENT/'mlp_shared_v1'
OUT=PARENT/'shared_capacity_search_v1'
FEATURES=kp.STAGE2+COLUMNS+TEMPORAL_COLUMNS+['farm']
KEYS=['origin','valid_at','lead','actual','site']


def main():
    OUT.mkdir(exist_ok=False)
    cfg=config()
    params=dict(cfg['lightgbm'],learning_rate=.03,n_estimators=1600)
    grid=[dict(num_leaves=leaves,min_child_samples=minimum) for leaves in [15,31,63,127] for minimum in [80,160]]
    protocol=dict(params=params,grid=grid,features=FEATURES,categorical_feature='farm',baseline=str(BASE),
        method='Eight predeclared pooled capacity configurations on original honest OOF periods',
        selection='Choose minimum clipped validation convex-blend MSE among eight configurations; freeze before one test evaluation',
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
    old=pl.read_csv(BASE/'validation_predictions.csv',try_parse_dates=True)
    if not va.select(KEYS).equals(old.select(KEYS)):raise ValueError('Validation keys/labels differ')
    baseline_v=old['mlp_blend'].to_numpy()
    x,y=tr.select(FEATURES).to_numpy(),tr['actual'].to_numpy()
    xv,yv=va.select(FEATURES).to_numpy(),va['actual'].to_numpy()
    records=[]; best=None
    for index,choice in enumerate(grid):
        estimator=LGBMRegressor(**dict(params,**choice))
        estimator.fit(x,y,categorical_feature=[len(FEATURES)-1],eval_set=[(xv,yv)],callbacks=[early_stopping(30,verbose=False)])
        prediction=np.clip(estimator.predict(xv),0,1)
        blend_weights=fit_weights(np.column_stack([baseline_v,prediction]),yv)
        score=metrics(yv,baseline_v*blend_weights[0]+prediction*blend_weights[1])
        records.append(dict(index=index,**choice,best_iteration=estimator.best_iteration_,validation_RMSE=score['RMSE'],weights=blend_weights.tolist()))
        (OUT/'validation_candidates.json').write_text(json.dumps(records,indent=2))
        if best is None or score['RMSE']<best:
            best=score['RMSE']; model=estimator; candidate=prediction; weights=blend_weights; selected=dict(choice)
            joblib.dump(dict(model=model,columns=FEATURES,protocol=protocol,selected=selected),OUT/'model.joblib')
        print(f'Capacity {index+1}/{len(grid)}: {choice}, validation blend RMSE={score["RMSE"]:.9f}',flush=True)
    (OUT/'weights.json').write_text(json.dumps(dict(baseline=float(weights[0]),capacity=float(weights[1]),selected=selected,train_samples=tr.height,validation_samples=va.height,best_iteration=model.best_iteration_),indent=2))
    if weights[1]<1e-8:
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
        pl.Series('capacity',np.clip(model.predict(te.select(FEATURES).to_numpy()),0,1)))
    test=test.with_columns((pl.col('baseline')*weights[0]+pl.col('capacity')*weights[1]).alias('capacity_blend'))
    scores=[dict(model=n,**metrics(test['actual'],test[n])) for n in ['baseline','capacity','capacity_blend']]
    paired=paired_comparison(test,'capacity_blend')
    accepted=scores[-1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:
        test.write_csv(OUT/'predictions.csv')
        va.select(KEYS).with_columns(pl.Series('baseline',baseline_v),pl.Series('capacity',candidate),
            pl.Series('capacity_blend',baseline_v*weights[0]+candidate*weights[1])).write_csv(OUT/'validation_predictions.csv')
    print(json.dumps(dict(accepted=accepted,weights=weights.tolist(),scores=scores,paired=paired),indent=2),flush=True)


if __name__=='__main__':main()
