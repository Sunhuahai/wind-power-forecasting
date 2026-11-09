"""Independent experiment runner; never writes legacy artifacts/latest_runs.json."""
from datetime import datetime
from pathlib import Path
from time import perf_counter
import json
import platform
import re
import uuid
import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor, early_stopping
from sqlalchemy import select
from windpower.db.core import engine, health, upsert
from windpower.db.schema import model_runs, predictions
from windpower.features.build import C, build
from windpower.models.train import config, partitions
from windpower.models.physics import PhysicsCurve, PhysicsGuidedModel
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

NAMES=['baseline','physics','residual','injected']

def write_json(path, value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False,default=str),encoding='utf-8')

def fit_suite(train, validation, baseline, model_cfg, experiment_cfg):
    """No test parameter: only train/validation can influence learned parameters."""
    x=train.select(C).to_numpy(); y=train['actual'].to_numpy()
    vx=validation.select(C).to_numpy(); vy=validation['actual'].to_numpy()
    speed=np.hypot(train['u100'].to_numpy(),train['v100'].to_numpy())
    start=perf_counter();curve=PhysicsCurve.fit(speed,y,experiment_cfg['physics']);physics_seconds=perf_counter()-start
    phy=PhysicsGuidedModel('physics',curve)
    p,pv=phy.predict(x),phy.predict(vx)
    models={'baseline':PhysicsGuidedModel('baseline',estimator=baseline),'physics':phy}
    timing={'physics':dict(train_seconds=physics_seconds,physical_fit_seconds=physics_seconds)}
    # Controlled refit measures cost of the old algorithm. It does not replace it.
    start=perf_counter();control=LGBMRegressor(**model_cfg['lightgbm'])
    control.fit(x,y,eval_set=[(vx,vy)],callbacks=[early_stopping(30,verbose=False)])
    timing['baseline']=dict(train_seconds=perf_counter()-start,physical_fit_seconds=0.,
        timing_source='controlled same-config refit; original historical time unavailable',
        validation_refit_max_abs_diff=float(np.max(np.abs(control.predict(vx)-baseline.predict(vx)))))
    for mode in ['residual','injected'] if experiment_cfg['feature_injection'] else ['residual']:
        tx,tvx=x,vx;ty,tvy=y,vy
        if mode=='residual':ty,tvy=y-p,vy-pv
        else:tx,tvx=np.column_stack([x,p]),np.column_stack([vx,pv])
        est=LGBMRegressor(**model_cfg['lightgbm']);start=perf_counter()
        est.fit(tx,ty,eval_set=[(tvx,tvy)],callbacks=[early_stopping(30,verbose=False)])
        booster_seconds=perf_counter()-start
        models[mode]=PhysicsGuidedModel(mode,curve,est)
        timing[mode]=dict(train_seconds=physics_seconds+booster_seconds,
                         physical_fit_seconds=physics_seconds,booster_train_seconds=booster_seconds)
    return models,timing,control

def footprint(model):
    """Comparable scalar values: tree leaf outputs + split thresholds, plus curve's 3.

    Counts prediction-active trees, not discarded early-stopping rounds. This is
    not neural-network parameter count and excludes topology/feature IDs.
    """
    trees=leaves=splits=0
    def count(node):
        if 'leaf_value' in node:return 1,0
        a,b=count(node['left_child']);c,d=count(node['right_child']);return a+c,b+d+1
    if model.estimator is not None:
        dump=model.estimator.booster_.dump_model(num_iteration=model.estimator.best_iteration_)
        trees=len(dump['tree_info'])
        for tree in dump['tree_info']:
            a,b=count(tree['tree_structure']);leaves+=a;splits+=b
    physics_parameters=0 if model.curve is None else 3
    return dict(active_trees=trees,leaf_values=leaves,split_thresholds=splits,
                physical_parameters=physics_parameters,scalar_parameters=leaves+splits+physics_parameters)

def inference_timing(model,x,settings):
    for _ in range(settings['warmup']):model.predict(x)
    times=[]
    for _ in range(settings['repeats']):
        start=perf_counter();model.predict(x);times.append(perf_counter()-start)
    median=float(np.median(times))
    return dict(inference_batch_seconds=median,inference_us_per_hour=median/len(x)*1e6,
                inference_ms_per_24h=median/len(x)*24000,
                timing_batch_samples=len(x),inference_repeats=len(times))

def assert_disjoint(parts):
    keys={name:set(f['valid_at'].to_list()) for name,f in parts.items()}
    if any(keys[a]&keys[b] for a,b in [('train','validation'),('train','test'),('validation','test')]):
        raise ValueError('Target labels cross partition boundaries')
    for f in parts.values():
        if f.filter((pl.col('valid_at')<=pl.col('origin'))|(~pl.col('lead').is_between(1,24))).height:
            raise ValueError('Invalid forecast origin/lead')

def run_site(site,folder,experiment_id,cfg,ecfg):
    out=folder/f'site_{site}'
    if (out/'complete.json').exists():
        status=json.loads((out/'complete.json').read_text())
        if status['site']!=site:raise ValueError('Invalid completion manifest')
        return
    out.mkdir(exist_ok=False)
    parts=partitions(build(site));assert_disjoint(parts)
    baseline_ids=json.loads(Path(f'artifacts/site_{site}/latest_runs.json').read_text())
    with engine().connect() as c:
        source=c.execute(select(model_runs).where(model_runs.c.id==baseline_ids[-1],model_runs.c.farm_id==site)).mappings().one()
    if source['model']!='lightgbm' or source['feature_group']!='C':raise ValueError('Need frozen original LightGBM C')
    old=joblib.load(source['artifact'])
    if old['columns']!=C or old['metadata']['config']!=cfg:raise ValueError('Baseline features/config differ')
    models,timing,control=fit_suite(parts['train'],parts['validation'],old['model'],cfg,ecfg)
    # Test predictions only start after all parameters and stopping rounds are fixed.
    test=parts['test'];x=test.select(C).to_numpy();y=test['actual'].to_numpy()
    expected=pl.read_csv(f'artifacts/site_{site}/{source["id"]}_predictions.csv',try_parse_dates=True)
    if expected.select('origin','valid_at').rows()!=test.select('origin','valid_at').rows():
        raise ValueError('Test samples differ from frozen baseline')
    if not np.array_equal(expected['actual'].to_numpy(),y):raise ValueError('Test labels differ')
    actual_baseline=models['baseline'].predict(x)
    if not np.allclose(actual_baseline,expected['prediction'].to_numpy(),atol=1e-14,rtol=0):
        raise ValueError('Frozen baseline predictions differ')
    if not np.isclose(metrics(y,actual_baseline)['RMSE'],source['metrics']['RMSE'],atol=1e-14,rtol=0):
        raise ValueError('Baseline database metric differs')
    curve=models['physics'].curve
    write_json(out/'physics_parameters.json',dict(**curve.parameters(),settings=ecfg['physics'],
        fit_partition='train only',fit_samples=parts['train'].height,
        train_MSE=float(np.mean((models['physics'].predict(parts['train'].select(C).to_numpy())-parts['train']['actual'].to_numpy())**2))))
    metadata=dict(experiment_id=experiment_id,source_baseline_id=source['id'],model_config=cfg,
        experiment_config=ecfg,columns=C,partitions={k:dict(samples=f.height,first=str(f['valid_at'].min()),last=str(f['valid_at'].max())) for k,f in parts.items()},
        availability='Source-clock 00:00; actual NWP issuance/arrival unknown. Same original assumption.',
        physics_parameters=curve.parameters(),history='Strictly before origin; original feature builder unchanged')
    records=[];resources=[];lead_scores=[];wind_scores=[]
    wide=test.select('origin','valid_at','lead','actual','speed100').with_columns(pl.lit(site).alias('site'))
    for name,model in models.items():
        start=perf_counter();pred=model.predict(x);first_seconds=perf_counter()-start
        if not np.isfinite(pred).all() or ((pred<0)|(pred>1)).any():raise ValueError('Invalid output')
        wide=wide.with_columns(pl.Series(name,pred));score=metrics(y,pred)
        rid=str(uuid.uuid4());path=out/f'{name}.joblib'
        joblib.dump(dict(model=model,columns=C,metadata=metadata),path)
        info=dict(site=site,model=name,run_id=rid,**score);records.append(info)
        resource=dict(site=site,model=name,**timing[name],**footprint(model),
            **inference_timing(model,x,ecfg['inference']),artifact_bytes=path.stat().st_size,
            first_prediction_seconds=first_seconds,
            best_iteration=0 if model.estimator is None else model.estimator.best_iteration_)
        if name=='baseline':resource['test_refit_max_abs_diff']=float(np.max(np.abs(np.clip(control.predict(x),0,1)-pred)))
        resources.append(resource)
        with engine().begin() as c:
            upsert(c,model_runs,[dict(id=rid,farm_id=site,model='physics_experiment_'+name,
                feature_group='C+physics' if name=='injected' else ('speed100' if name=='physics' else 'C'),
                created_at=datetime.now(),config=dict(**metadata,resources=resource),artifact=str(path),metrics=score)])
            rows=test.select('origin','valid_at','lead','actual').with_columns(pl.Series('prediction',pred)).to_dicts()
            upsert(c,predictions,[dict(run_id=rid,**r) for r in rows])
        for h in range(1,25):
            mask=test['lead'].to_numpy()==h
            lead_scores.append(dict(site=site,model=name,lead=h,**metrics(y[mask],pred[mask])))
        bounds=ecfg['wind_bins']+[float('inf')];speed=test['speed100'].to_numpy()
        for a,b in zip(bounds,bounds[1:]):
            mask=(speed>=a)&(speed<b)
            if not mask.any():continue
            label=f'[{a},{b if np.isfinite(b) else "inf"})'
            wind_scores.append(dict(site=site,model=name,wind_bin=label,lower=a,upper=b if np.isfinite(b) else None,**metrics(y[mask],pred[mask])))
    wide.write_csv(out/'predictions.csv')
    pl.DataFrame(records).write_csv(out/'metrics.csv')
    write_json(out/'resources.json',resources);write_json(out/'metadata.json',metadata)
    pl.DataFrame(lead_scores).write_csv(out/'lead_metrics.csv');pl.DataFrame(wind_scores).write_csv(out/'wind_metrics.csv')
    paired=[dict(site=site,**paired_comparison(wide,name,**ecfg['bootstrap'])) for name in models if name!='baseline']
    pl.DataFrame(paired).write_csv(out/'paired_comparisons.csv')
    from windpower.evaluation.physics_report import site_figures
    site_figures(site,out,wide,curve,parts['train'])
    write_json(out/'complete.json',dict(site=site,experiment_id=experiment_id,run_ids={r['model']:r['run_id'] for r in records},test_samples=test.height))
    print(f'Zone {site}: '+', '.join(f'{r["model"]} RMSE={r["RMSE"]:.6f}' for r in records),flush=True)

def run_experiment(sites,experiment_id=None):
    cfg=config();ecfg=json.loads(Path('configs/physics-experiment.json').read_text())
    experiment_id=experiment_id or datetime.now().strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:8]
    if not re.fullmatch(r'[A-Za-z0-9_-]+',experiment_id):raise ValueError('Invalid experiment ID')
    folder=Path('artifacts/physics_experiments')/experiment_id
    manifest=folder/'manifest.json'
    if manifest.exists():
        saved=json.loads(manifest.read_text())
        if saved['model_config']!=cfg or saved['experiment_config']!=ecfg:
            raise ValueError('Cannot change a frozen experiment configuration')
    else:
        folder.mkdir(parents=True,exist_ok=False)
        write_json(manifest,dict(experiment_id=experiment_id,created_at=datetime.now(),postgres=health(),
            model_config=cfg,experiment_config=ecfg,platform=platform.platform(),python=platform.python_version(),
            protocol='Train-only physical fit; frozen baseline; no test-guided changes; paired 7-calendar-day circular block bootstrap'))
    for site in sites:
        if not 1<=site<=10:raise ValueError('site must be 1..10')
        run_site(site,folder,experiment_id,cfg,ecfg)
    from windpower.evaluation.physics_report import summarize_experiment
    summarize_experiment(folder,ecfg)
    return folder
