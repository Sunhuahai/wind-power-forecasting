from datetime import datetime,timedelta
from pathlib import Path
import json
import numpy as np
import polars as pl
import pytest
from windpower.features.build import C
from windpower.models.physics import PhysicsCurve,PhysicsGuidedModel
from windpower.models.physics_experiment import fit_suite,assert_disjoint
from windpower.evaluation.paired import paired_comparison

SETTINGS=json.loads(Path('configs/physics-experiment.json').read_text())

class Constant:
    def __init__(self,value):self.value=value
    def predict(self,x):return np.full(len(x),self.value)

def test_curve_boundary_continuity_and_large_speed():
    curve=PhysicsCurve(3,12,.8)
    got=curve.predict(np.array([0,3,3+1e-7,12-1e-7,12,10000]))
    assert got[0]==got[1]==0
    assert 0<got[2]<1e-6 and abs(got[3]-.8)<1e-6
    assert np.allclose(got[4:],[.8,.8])
    assert (np.diff(got)>=0).all() and ((got>=0)&(got<=1)).all()

@pytest.mark.parametrize('args',[(3,3,.8),(-1,12,.8),(3,12,1.1),(3,float('nan'),.8)])
def test_bad_curve_parameters(args):
    with pytest.raises(ValueError):PhysicsCurve(*args)

def test_fit_parameters_bounded_and_recovers_training_curve():
    speed=np.linspace(0,24,250);target=PhysicsCurve(2,10,.8).predict(speed)
    result=PhysicsCurve.fit(speed,target,SETTINGS['physics'])
    assert 0<=result.cut_in<=6 and 6.5<=result.rated<=20 and .3<=result.plateau<=1
    assert result.cut_in==2 and result.rated==10
    assert np.allclose(result.predict(speed),target,atol=1e-12)

@pytest.mark.parametrize('speed,actual',[([],[]),([1],[np.nan]),([-1],[.3]),([1],[1.1])])
def test_invalid_fit_samples(speed,actual):
    with pytest.raises(ValueError):PhysicsCurve.fit(speed,actual,SETTINGS['physics'])

def test_residual_sum_then_clip_and_injected_input():
    x=np.zeros((4,len(C)));x[:,C.index('u100')]=12
    curve=PhysicsCurve(3,12,.6)
    assert np.allclose(PhysicsGuidedModel('residual',curve,Constant(-.3)).predict(x),.3)
    assert np.all(PhysicsGuidedModel('residual',curve,Constant(-1)).predict(x)==0)
    assert np.all(PhysicsGuidedModel('residual',curve,Constant(2)).predict(x)==1)
    class Injection:
        def predict(self,data):
            assert data.shape[1]==26
            assert np.allclose(data[:,-1],.6)
            return data[:,-1]
    assert np.allclose(PhysicsGuidedModel('injected',curve,Injection()).predict(x),.6)

def test_fit_uses_training_labels_only_and_validation_only_for_early_stop(monkeypatch):
    import windpower.models.physics_experiment as experiment
    records=[]
    class Recording:
        def __init__(self,**kwargs):pass
        def fit(self,x,y,eval_set,callbacks):
            records.append((x.copy(),y.copy(),eval_set[0][0].copy(),eval_set[0][1].copy()))
        def predict(self,x):return np.zeros(len(x))
    monkeypatch.setattr(experiment,'LGBMRegressor',Recording)
    def frame(n,start):
        fields={k:np.full(n,.1) for k in C};fields['u100']=np.linspace(1,15,n);fields['v100']=np.zeros(n)
        fields['actual']=np.linspace(.01,.9,n)
        return pl.DataFrame(fields)
    train=frame(40,0);validation=frame(12,40)
    a,_,_=fit_suite(train,validation,Constant(0),{'lightgbm':{}},SETTINGS)
    first_records=records.copy();records.clear()
    # Alter all nontraining labels. Physics parameters must be unchanged.
    changed_validation=validation.with_columns(pl.lit(.99).alias('actual'))
    b,_,_=fit_suite(train,changed_validation,Constant(0),{'lightgbm':{}},SETTINGS)
    assert a['physics'].curve==b['physics'].curve
    assert len(first_records)==3  # controlled C refit, residual, direct injection
    tx,ty,vx,vy=first_records[1]
    assert tx.shape==(40,25) and vx.shape==(12,25)
    assert np.allclose(ty,train['actual'].to_numpy()-a['physics'].predict(tx))
    assert np.allclose(vy,validation['actual'].to_numpy()-a['physics'].predict(vx))
    assert first_records[2][0].shape==(40,26)
    assert np.array_equal(first_records[2][1],train['actual'].to_numpy())
    # There is no test input path to the fitting function.
    import inspect
    assert 'test' not in inspect.signature(fit_suite).parameters


def test_partition_overlap_rejected():
    t=datetime(2013,1,1)
    def sample(t):return pl.DataFrame({'origin':[t],'valid_at':[t+timedelta(hours=1)],'lead':[1]})
    with pytest.raises(ValueError):assert_disjoint({'train':sample(t),'validation':sample(t),'test':sample(t+timedelta(days=2))})

def test_paired_bootstrap_identical_models_and_calendar_gaps():
    start=datetime(2013,7,1)
    f=pl.DataFrame({'origin':[start,start+timedelta(days=2)],'actual':[.2,.7],'baseline':[.3,.8],'residual':[.3,.8]})
    r=paired_comparison(f,'residual',replicates=100,block_days=2)
    assert r['calendar_days']==3 and r['n']==2
    assert r['delta_MAE']==r['delta_RMSE']==r['delta_RMSE_low']==r['delta_RMSE_high']==0
