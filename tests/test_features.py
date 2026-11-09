from datetime import datetime,timedelta
import numpy as np
from windpower.features.build import feature_row,A,split
from windpower.data.ingest import origin_for,clean,timestamp
from windpower.evaluation.metrics import metrics
import polars as pl

def test_midnight_is_previous_days_lead24():
    t=datetime(2013,1,2)
    assert origin_for(t)==datetime(2013,1,1)
    assert timestamp('20130102 0:00')==t

def test_future_and_origin_power_cannot_leak():
    origin=datetime(2013,1,2);valid=origin+timedelta(hours=3)
    power={origin-timedelta(hours=k):k/24 for k in range(1,25)}
    before=feature_row(valid,dict(zip(A,[3.,4.,6.,8.])),power)
    power.update({origin:999,valid:999,origin+timedelta(hours=1):999})
    after=feature_row(valid,dict(zip(A,[3.,4.,6.,8.])),power)
    assert before==after
    assert before['speed100']==10 and before['lag1']==1/24
    assert before['lead']==3

def test_missing_exact_history_rejected():
    t=datetime(2013,1,2,1)
    assert feature_row(t,dict(zip(A,[1.,2.,3.,4.])),{}) is None

def test_split_never_crosses_boundary():
    start=datetime(2013,1,1);end=datetime(2013,1,2)
    f=pl.DataFrame({'origin':[start-timedelta(days=1),start,start], 'valid_at':[start,start+timedelta(hours=1),end+timedelta(hours=1)]})
    assert split(f,start,end).height==1

def test_quality_and_metrics():
    assert clean(float('inf'))==(None,'nonfinite')
    assert clean(1.1,True)==(None,'outside_0_1')
    m=metrics([0,1],[0,0]);assert m['MAE']==m['nMAE']==.5
    assert np.isclose(m['RMSE'],np.sqrt(.5))

def test_pooled_errors_weight_different_site_counts():
    from windpower.evaluation.metrics import pooled_metrics
    result=pooled_metrics([dict(n=1,MAE=0,RMSE=0),dict(n=3,MAE=1,RMSE=1)])
    assert result['MAE']==.75
    assert np.isclose(result['RMSE'],np.sqrt(.75))
