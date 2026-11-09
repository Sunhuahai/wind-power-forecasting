"""Actual PostgreSQL acceptance audit; reruns one import and prediction for idempotency."""
import json
from pathlib import Path
from sqlalchemy import text
from windpower.db.core import engine,health
from windpower.data.ingest import ingest
from windpower.evaluation.run import predict

def counts():
    with engine().connect() as c:
        return {table:c.scalar(text(f'SELECT count(*) FROM {table}')) for table in
                ['wind_farms','power_observations','nwp_forecasts','dataset_splits','model_runs','predictions']}
before=counts();ingest(1);predict(1);after=counts()
assert before==after,(before,after)
with engine().connect() as c:
    rows=[dict(r) for r in c.execute(text('''SELECT farm_id,count(*) AS rows,
      count(*) FILTER (WHERE power IS NULL) AS missing FROM power_observations GROUP BY farm_id ORDER BY farm_id''')).mappings()]
    bad=c.scalar(text('''SELECT count(*) FROM predictions WHERE lead NOT BETWEEN 1 AND 24
      OR valid_at <> origin+lead*interval '1 hour' OR prediction<0 OR prediction>1'''))
    example=[dict(r) for r in c.execute(text('''SELECT r.farm_id,r.model,p.origin,p.valid_at,p.lead,p.prediction
      FROM predictions p JOIN model_runs r ON r.id=p.run_id ORDER BY r.created_at DESC,p.origin,p.lead LIMIT 24''')).mappings()]
    unknown=c.scalar(text('SELECT count(*) FROM nwp_forecasts WHERE issued_at IS NULL'))
assert bad==0
report=dict(postgres=health(),counts=after,idempotent_import_and_prediction=before==after,
            invalid_prediction_rows=bad,source_by_site=rows,unknown_issued_at=unknown,example=example)
Path('artifacts/acceptance.json').write_text(json.dumps(report,indent=2,default=str))
print(json.dumps({k:v for k,v in report.items() if k!='example'},indent=2,default=str))
