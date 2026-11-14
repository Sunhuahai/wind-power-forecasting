from datetime import datetime, timedelta
import polars as pl
import pytest
from windpower.features.cross_nwp import spatial_panel, attach_spatial, COLUMNS
from windpower.features.cross_nwp import temporal_spatial_panel, attach_temporal
from windpower.features.cross_nwp import vertical_spatial_panel, attach_vertical, VERTICAL_COLUMNS


def test_only_forecast_fields_and_exact_origins():
    origin = datetime(2013, 1, 1)
    keys = pl.DataFrame(dict(origin=[origin,origin+timedelta(days=1)],
                             valid_at=[origin+timedelta(hours=1),origin+timedelta(days=1,hours=1)]))
    panels = {site:keys.with_columns(pl.lit(float(site)).alias('u100'),
               pl.lit(0.).alias('v100'),pl.lit(float(site)).alias('speed100'),
               pl.lit(999.).alias('actual')) for site in range(1,11)}
    panel = spatial_panel(panels)
    assert set(panel.columns) == set(['origin','valid_at']+COLUMNS)
    assert attach_spatial(keys,panel)['nwp_2_speed100'].to_list() == [2.,2.]
    wrong_origin = keys.head(1).with_columns(pl.col('origin')+timedelta(days=1))
    with pytest.raises(ValueError,match='Missing or nonfinite'):
        attach_spatial(wrong_origin,panel)
    with pytest.raises(ValueError,match='ten'):
        spatial_panel({1:panels[1]})


def test_temporal_wind_never_crosses_origin():
    first = datetime(2013,1,1)
    rows = [dict(origin=first+timedelta(days=day),valid_at=first+timedelta(days=day,hours=lead),
                 speed100=1. if day==0 else 8.,u100=1.,v100=0.)
            for day in range(2) for lead in range(1,25)]
    panels = {site:pl.DataFrame(rows) for site in range(1,11)}
    combined = temporal_spatial_panel(panels)
    result = attach_temporal(pl.DataFrame(rows).select('origin','valid_at'),combined)
    assert result['nwp_1_speed_shift_+3'][23] == 1.
    assert result['nwp_10_speed_shift_-3'][24] == 8.
    assert result['nwp_2_speed_gradient'].to_list() == [0.]*48
    with pytest.raises(ValueError,match='complete 24-hour'):
        temporal_spatial_panel({site:frame.head(23) for site,frame in panels.items()})


def test_vertical_fields_whitelisted_and_original_features_unchanged():
    origin=datetime(2013,1,1)
    rows=[dict(origin=origin,valid_at=origin+timedelta(hours=h),u100=3.,v100=4.,speed100=5.,
               u10=0.,v10=2.,speed10=2.,shear=.4,actual=.99) for h in range(1,25)]
    panels={site:pl.DataFrame(rows) for site in range(1,11)}
    old=temporal_spatial_panel(panels)
    new=vertical_spatial_panel(panels)
    assert new.select(old.columns).equals(old)
    assert set(new.columns)-set(old.columns)==set(VERTICAL_COLUMNS)
    joined=attach_vertical(pl.DataFrame(rows).select('origin','valid_at'),new)
    assert joined['nwp_10_speed10'].to_list()==[2.]*24
    assert not any('actual' in c for c in new.columns)
