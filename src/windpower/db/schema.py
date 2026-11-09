from sqlalchemy import MetaData, Table, Column, Integer, String, Float, DateTime, ForeignKey, JSON, Index, CheckConstraint
metadata = MetaData()
wind_farms = Table('wind_farms', metadata,
    Column('id', Integer, primary_key=True), Column('name', String, nullable=False),
    Column('power_unit', String, nullable=False), Column('time_semantics', String, nullable=False))
power_observations = Table('power_observations', metadata,
    Column('farm_id', ForeignKey('wind_farms.id'), primary_key=True),
    Column('observed_at', DateTime, primary_key=True), Column('power', Float),
    Column('raw_timestamp', String, nullable=False), Column('quality', JSON, nullable=False),
    CheckConstraint('power IS NULL OR (power >= 0 AND power <= 1)'))
nwp_forecasts = Table('nwp_forecasts', metadata,
    Column('farm_id', ForeignKey('wind_farms.id'), primary_key=True),
    Column('valid_at', DateTime, primary_key=True),
    Column('assumed_origin', DateTime, nullable=False), Column('issued_at', DateTime),
    Column('raw_timestamp', String, nullable=False), Column('u10', Float), Column('v10', Float),
    Column('u100', Float), Column('v100', Float), Column('quality', JSON, nullable=False),
    Column('availability', String, nullable=False))
dataset_splits = Table('dataset_splits', metadata,
    Column('name', String, primary_key=True), Column('start_at', DateTime, nullable=False),
    Column('end_at', DateTime, nullable=False), Column('semantics', String, nullable=False))
model_runs = Table('model_runs', metadata,
    Column('id', String, primary_key=True), Column('farm_id', ForeignKey('wind_farms.id'), nullable=False),
    Column('model', String, nullable=False), Column('feature_group', String, nullable=False),
    Column('created_at', DateTime, nullable=False), Column('config', JSON, nullable=False),
    Column('artifact', String, nullable=False), Column('metrics', JSON))
predictions = Table('predictions', metadata,
    Column('run_id', ForeignKey('model_runs.id'), primary_key=True),
    Column('origin', DateTime, primary_key=True), Column('valid_at', DateTime, primary_key=True),
    Column('lead', Integer, nullable=False), Column('prediction', Float, nullable=False),
    Column('actual', Float), CheckConstraint('lead BETWEEN 1 AND 24'),
    CheckConstraint('prediction >= 0 AND prediction <= 1'))
Index('ix_power_time', power_observations.c.observed_at)
Index('ix_nwp_time', nwp_forecasts.c.valid_at)
Index('ix_prediction_time', predictions.c.valid_at)
