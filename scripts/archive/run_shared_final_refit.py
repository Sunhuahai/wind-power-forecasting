"""Frozen-weight replacement of the shared branch after final past-data refit."""
from datetime import datetime
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor

from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel,attach_temporal
from windpower.models.train import config,partitions
from windpower.models import kpower_reproduction as kp
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID='ablation_20261114_v1'
PARENT=Path('artifacts/kpower_ablation')/ID
OUT=PARENT/'shared_final_refit_v1'
KEYS=['origin','valid_at','lead','actual','site']


def past_oof(train,panel,params,site):
    origins=train['origin'].unique().sort().to_list()
    blocks=[]; audit=[]
    for start in range(kp.MIN_TRAIN_DAYS,len(origins),kp.OOF_BLOCK_DAYS):
        cutoff=origins[start]
        tr=train.filter((pl.col('origin')<cutoff)&(pl.col('valid_at')<cutoff))
        hold=panel.filter(pl.col('origin').is_in(origins[start:start+kp.OOF_BLOCK_DAYS]))
        if tr.is_empty() or hold.is_empty():continue
        if tr['valid_at'].max()>=hold['origin'].min():raise ValueError('OOF label availability violation')
        model=kp.qmodel(params,.5,kp.OOF_TREES)
        model.fit(tr.select(kp.STAGE1).to_numpy(),tr['actual'].to_numpy())
        blocks.append(hold.select('origin','valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(model.predict(hold.select(kp.STAGE1).to_numpy()),0,1))))
        audit.append(dict(site=site,train_max=str(tr['valid_at'].max()),origin_min=str(hold['origin'].min()),samples=tr.height))
    return pl.concat(blocks).sort('origin','valid_at'),audit


def main():
    registry=json.loads(Path('docs/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id']!='mlp_shared_blend':raise ValueError('Baseline changed')
    OUT.mkdir(exist_ok=False)
    cfg=config(); cutoff=datetime.fromisoformat(cfg['splits']['test'][0])
    shared=joblib.load(PARENT/'shared_model_v1/model.joblib')
    columns=shared['columns']; rounds=shared['model'].best_iteration_
    params=dict(shared['model'].get_params(),n_estimators=rounds)
    first=joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib')))['stage1']
    first_rounds={site:first[site].best_iteration_ for site in range(1,11)}
    weights={name:json.loads((PARENT/name/'weights.json').read_text()) for name in ['shared_model_v1','curve_residual_v1','mlp_shared_v1']}
    coefficient=weights['shared_model_v1']['shared']*weights['curve_residual_v1']['baseline']*weights['mlp_shared_v1']['baseline']
    protocol=dict(method='Replace only the accepted shared branch after final train+validation refit',
        cutoff=str(cutoff),label_rule='All final fitting labels strictly before first test origin',
        rounds=rounds,first_rounds=first_rounds,fixed_branch_coefficient=coefficient,
        selection='No new model/round/weight selection; original selected rounds and combination coefficients frozen',
        protocol_change='Former validation labels become final fitting data; no independent validation remains for this refit',
        caveat='Exploratory fixed historical test; NWP issue and observation arrival timestamps unknown')
    (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    panels,parts,extended,stage1,oofs,crosses,audit={},{},{},{},[],[],[]
    for site in range(1,11):
        panel=kp.nwp_panel(site);panels[site]=panel
        parts[site]={k:kp.attach(v,panel) for k,v in partitions(build(site)).items()}
        extended[site]=pl.concat([parts[site]['train'],parts[site]['validation']]).filter(pl.col('valid_at')<cutoff).sort('origin','valid_at')
        stage1[site]=kp.qmodel(cfg['lightgbm'],.5,first_rounds[site])
        stage1[site].fit(extended[site].select(kp.STAGE1).to_numpy(),extended[site]['actual'].to_numpy())
        oof,proof=past_oof(extended[site],panel,cfg['lightgbm'],site)
        oofs.append(oof);audit.extend(proof)
        crosses.append(panel.select('origin','valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()),0,1))))
        print(f'Final-refit first stage zone {site}',flush=True)
    (OUT/'oof_audit.json').write_text(json.dumps(audit,indent=2))
    spatial=temporal_spatial_panel(panels)
    train_cross,test_cross=kp.combine(oofs),kp.combine(crosses)
    trains=[]
    for site in range(1,11):
        tr=extended[site].join(train_cross,on=['origin','valid_at'],how='inner',validate='1:1').sort('origin','valid_at')
        trains.append(attach_temporal(tr,spatial).with_columns(pl.lit(site).alias('farm')))
    tr=pl.concat(trains)
    if tr['valid_at'].max()>=cutoff:raise ValueError('Final training crosses test origin')
    model=LGBMRegressor(**params)
    model.fit(tr.select(columns).to_numpy(),tr['actual'].to_numpy(),categorical_feature=[len(columns)-1])
    joblib.dump(dict(model=model,stage1=stage1,columns=columns,protocol=protocol),OUT/'model.joblib')
    (OUT/'weights.json').write_text(json.dumps(dict(shared_branch=coefficient,frozen_rounds=rounds,
        train_samples=tr.height,train_first=str(tr['valid_at'].min()),train_last=str(tr['valid_at'].max())),indent=2))
    tests=[]
    for site in range(1,11):
        te=attach_temporal(kp.add_cross_site(parts[site]['test'],test_cross),spatial).with_columns(pl.lit(site).alias('farm'),pl.lit(site).alias('site'))
        tests.append(te)
    te=pl.concat(tests)
    previous=pl.read_csv(PARENT/'mlp_shared_v1/predictions.csv',try_parse_dates=True)
    old_shared=pl.read_csv(PARENT/'shared_model_v1/predictions.csv',try_parse_dates=True)
    if not te.select(KEYS).equals(previous.select(KEYS)) or not te.select(KEYS).equals(old_shared.select(KEYS)):
        raise ValueError('Test keys/labels differ')
    replacement=np.clip(model.predict(te.select(columns).to_numpy()),0,1)
    retained=previous['mlp_blend'].to_numpy()-coefficient*old_shared['shared'].to_numpy()
    if (retained < -1e-12).any() or (retained>1-coefficient+1e-12).any():raise ValueError('Invalid branch decomposition')
    result=te.select(KEYS).with_columns(pl.Series('baseline',previous['mlp_blend']),pl.Series('refit_shared',replacement),
        pl.Series('final_refit',np.clip(retained+coefficient*replacement,0,1)))
    scores=[dict(model=n,**metrics(result['actual'],result[n])) for n in ['baseline','refit_shared','final_refit']]
    paired=paired_comparison(result,'final_refit')
    accepted=scores[-1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:result.write_csv(OUT/'predictions.csv')
    print(json.dumps(dict(accepted=accepted,scores=scores,paired=paired),indent=2),flush=True)


if __name__=='__main__':main()
