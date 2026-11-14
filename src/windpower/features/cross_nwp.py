"""Forecast-only spatial wind field, joined on both origin and valid time."""
import numpy as np
import polars as pl

VARIABLES = ['u100', 'v100', 'speed100']
VERTICAL_VARIABLES = ['u10', 'v10', 'speed10', 'shear']
VERTICAL_COLUMNS = [f'nwp_{site}_{name}' for site in range(1,11) for name in VERTICAL_VARIABLES]
COLUMNS = [f'nwp_{site}_{name}' for site in range(1, 11) for name in VARIABLES]
KEYS = ['origin', 'valid_at']
SHIFTS = [-3, -1, 1, 3]
TEMPORAL_COLUMNS = [f'nwp_{site}_speed_shift_{offset:+d}' for site in range(1, 11) for offset in SHIFTS]
TEMPORAL_COLUMNS += [f'nwp_{site}_speed_gradient' for site in range(1, 11)]


def spatial_panel(panels, vertical=False):
    if set(panels) != set(range(1, 11)):
        raise ValueError('Need ten forecast panels')
    combined = None
    for site in range(1, 11):
        # Whitelist NWP fields; never forward power labels or histories.
        variables = VARIABLES + (VERTICAL_VARIABLES if vertical else [])
        frame = panels[site].select(KEYS + variables).rename(
            {name: f'nwp_{site}_{name}' for name in variables})
        combined = frame if combined is None else combined.join(frame, on=KEYS, how='inner', validate='1:1')
    return combined.sort(KEYS)


def attach_spatial(frame, panel, columns=COLUMNS):
    result = frame.join(panel, on=KEYS, how='left', validate='1:1').sort(KEYS)
    if not np.isfinite(result.select(columns).to_numpy()).all():
        raise ValueError('Missing or nonfinite cross-site NWP')
    return result


def temporal_spatial_panel(panels, vertical=False):
    combined = spatial_panel(panels,vertical=vertical)
    for site in range(1, 11):
        frame = panels[site].sort(KEYS)
        leads = frame.select(((pl.col('valid_at')-pl.col('origin')).dt.total_hours()).alias('lead'))['lead']
        counts = frame.group_by('origin').agg(pl.col('valid_at').n_unique().alias('n'))
        if not counts['n'].eq(24).all() or not leads.is_between(1,24).all():
            raise ValueError('Temporal NWP requires complete 24-hour forecast origins')
        for offset in SHIFTS:
            endpoint = pl.col('speed100').first() if offset < 0 else pl.col('speed100').last()
            frame = frame.with_columns(
                pl.col('speed100').shift(-offset).over('origin').fill_null(endpoint.over('origin'))
                .alias(f'nwp_{site}_speed_shift_{offset:+d}'))
        frame = frame.with_columns((pl.col(f'nwp_{site}_speed_shift_+1')-pl.col(f'nwp_{site}_speed_shift_-1'))
                                  .alias(f'nwp_{site}_speed_gradient'))
        columns = [c for c in TEMPORAL_COLUMNS if c.startswith(f'nwp_{site}_')]
        combined = combined.join(frame.select(KEYS+columns),on=KEYS,how='inner',validate='1:1')
    return combined.sort(KEYS)


def attach_temporal(frame, panel):
    return attach_spatial(frame,panel,columns=COLUMNS+TEMPORAL_COLUMNS)


def vertical_spatial_panel(panels):
    return temporal_spatial_panel(panels,vertical=True)


def attach_vertical(frame,panel):
    return attach_spatial(frame,panel,columns=COLUMNS+TEMPORAL_COLUMNS+VERTICAL_COLUMNS)
