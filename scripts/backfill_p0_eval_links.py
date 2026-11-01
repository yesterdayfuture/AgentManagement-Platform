"""Attach workflow run links to reports made before per-case run IDs were recorded."""
import json
import sqlite3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
database = root / "backend/data/app.db"
reports = root / "backend/reports"
with sqlite3.connect(database, timeout=30) as db:
    for path in sorted(reports.glob("p0-live-*.json")):
        if path.name == "p0-live-config.json":
            continue
        report = json.loads(path.read_text())
        details = report["details"]
        if all(item.get("run_id") for item in details):
            continue
        workflow_id = report["workflow_id"]
        completed_at = db.execute("SELECT created_at FROM eval_runs WHERE id = ?", (report["eval_run_id"],)).fetchone()
        if completed_at is None:
            raise RuntimeError(f"Missing eval run for {path.name}")
        runs = db.execute(
            "SELECT id, message, trace_id FROM workflow_runs WHERE workflow_id = ? AND started_at <= ? "
            "ORDER BY id DESC LIMIT ?",
            (workflow_id, completed_at[0], len(details)),
        ).fetchall()[::-1]
        if len(runs) != len(details) or any(run[1] != case["input"] for run, case in zip(runs, details)):
            raise RuntimeError(f"Cannot uniquely match workflow runs for {path.name}")
        for run, case in zip(runs, details):
            case["run_id"] = run[0]
            case["trace_id"] = run[2]
        db.execute("UPDATE eval_runs SET detail_json = ? WHERE id = ?", (json.dumps(details, ensure_ascii=False), report["eval_run_id"]))
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"Linked {len(details)} cases in {path.name}")
