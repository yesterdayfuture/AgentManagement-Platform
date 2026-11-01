"""Run one persisted P0 dataset using the visible DeepSeek agent and workflow."""
import argparse
import json
import time
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.models import ContextPolicy, EvalDataset, Space, User, Workflow
from app.services.runtime.evaluation import run_eval

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('fixture')
args = parser.parse_args()
config = json.loads((root / 'backend/reports/p0-live-config.json').read_text())
item = next(item for item in config['sets'] if item['fixture'] == args.fixture)
fixture = json.loads((root / 'backend/fixtures' / args.fixture).read_text())
engine = create_engine(f"sqlite:///{root / 'backend/data/app.db'}", connect_args={'check_same_thread': False, 'timeout': 30})
@event.listens_for(engine, 'connect')
def setup_connection(connection, _):
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=30000')
Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
db = Session()
started = time.monotonic()
try:
    admin = db.query(User).filter_by(username='admin').one()
    space = db.get(Space, config['space_id'])
    policy = db.query(ContextPolicy).filter_by(space_id=space.id).one()
    dataset = db.get(EvalDataset, item['dataset_id'])
    workflow = db.get(Workflow, item['workflow_id'])
    print(f"START {args.fixture} dataset={dataset.id} workflow={workflow.id} cases={item['case_count']}", flush=True)
    def progress(done, total, detail):
        if done % 25 == 0 or done == total:
            print(f"PROGRESS {args.fixture} {done}/{total} elapsed={time.monotonic()-started:.1f}s passed={detail['passed']} model_online={detail['model_online']}", flush=True)
    result = run_eval(db, dataset, admin, space, 'admin', policy, workflow=workflow, metrics=fixture['metrics'], progress=progress)
    db.commit()
    report = {
        'eval_run_id': result['id'], 'space_id': space.id, 'space_name': space.name,
        'agent_id': config['agent_id'], 'agent_name': config['agent_name'],
        'model_id': config['model_id'], 'model_name': config['model_name'], 'model_code': config['model_code'],
        'dataset_id': dataset.id, 'workflow_id': workflow.id, 'workflow_revision': result['workflow_revision'],
        'fixture': args.fixture, 'source': fixture['source'], 'license': fixture['license'],
        'selection': fixture['selection'], 'category': fixture['category'],
        'dataset_cases': item['case_count'], 'elapsed_seconds': round(time.monotonic()-started, 1),
        'pass_rate': result['completion'], 'cost': result['cost'], 'metrics': result['metrics'],
        'details': result['details'],
    }
    path = root / 'backend/reports' / f"p0-live-{Path(args.fixture).stem}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print('DONE', json.dumps({k: report[k] for k in ('fixture','eval_run_id','dataset_cases','elapsed_seconds','pass_rate','cost')}, ensure_ascii=False), flush=True)
except Exception:
    db.rollback()
    raise
finally:
    db.close()
    engine.dispose()
