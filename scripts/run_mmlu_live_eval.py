"""Run imported MMLU and text-editing datasets with the visible DeepSeek agent."""
import json
import time
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Agent, ContextPolicy, EvalDataset, Space, User
from app.services.runtime.evaluation import run_eval

root = Path(__file__).resolve().parents[1]
config = json.loads((root / "backend/reports/mmlu-live-config.json").read_text())
engine = create_engine(f"sqlite:///{root / 'backend/data/app.db'}", connect_args={"check_same_thread": False, "timeout": 30})

@event.listens_for(engine, "connect")
def setup_connection(connection, _):
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")

Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
try:
    for item in config["sets"]:
        fixture_name = item["fixture"]
        path = root / "backend/reports" / f"mmlu-live-{Path(fixture_name).stem}.json"
        if path.exists():
            print("SKIP", fixture_name, flush=True)
            continue
        fixture = json.loads((root / "backend/fixtures" / fixture_name).read_text())
        db = Session()
        started = time.monotonic()
        try:
            user = db.query(User).filter_by(username="admin").one()
            space = db.get(Space, config["space_id"])
            policy = db.query(ContextPolicy).filter_by(space_id=space.id).one()
            agent = db.get(Agent, config["agent_id"])
            dataset = db.get(EvalDataset, item["dataset_id"])
            print("START", fixture_name, len(fixture["cases"]), flush=True)
            def progress(done, total, detail):
                if done % 10 == 0 or done == total:
                    print("PROGRESS", fixture_name, done, total, "online", detail["model_online"], "elapsed", round(time.monotonic() - started, 1), flush=True)
            result = run_eval(db, dataset, user, space, "admin", policy, agent=agent,
                              metrics=fixture["metrics"], min_pass_rate=0.8, progress=progress)
            db.commit()
            report = {"eval_run_id": result["id"], "space_id": space.id, "space_name": space.name,
                      "agent_id": agent.id, "agent_name": agent.name, "model_code": config["model_code"],
                      "dataset_id": dataset.id, "fixture": fixture_name, "source": fixture["source"],
                      "license": fixture["license"], "category": fixture["category"],
                      "selection": fixture["selection"], "min_pass_rate": result["min_pass_rate"],
                      "elapsed_seconds": round(time.monotonic() - started, 1),
                      "pass_rate": result["completion"], "passed": result["passed"], "cost": result["cost"],
                      "metrics": result["metrics"], "details": result["details"]}
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print("DONE", fixture_name, result["id"], result["completion"], round(result["cost"], 6), flush=True)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
finally:
    engine.dispose()
