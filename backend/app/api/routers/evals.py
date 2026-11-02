"""评测集、用例和运行。用例可以逐条维护，也可以从表格解析。"""

import json
import queue
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, or_

from app.deps import RANK, get_context, get_db, get_principal, require
from app.models import (
    Agent, AgentVersion, Approval, AuditLog, Chunk, ContextPolicy, DelegationHop,
    DepartmentGrant, Document, EvalCase, EvalDataset, EvalRun, KnowledgeBase,
    LedgerAccount, Membership, McpServer, Memory, ModelEndpoint, Secret, Space,
    Tool, ToolGrant, Trace, TraceStep, User, Workflow, WorkflowRun,
)
from app.paging import Desk, apply_desk, desk_params, finish, finish_rows, stamp, window
from app.services.grants import drop_resource, grant, ids_for, is_platform, replace_grants, visible_ids
from app.security import channel_secret, hash_password, issue_token, sign_sso, verify_password
from app.services.builtins import detail_of
from app.services.gateway import GateError
from app.services.mcp_client import McpError, call_tool, connect
from app.services.ingest import IngestError, extract_text, split_text, strategy_catalog, suggest_strategy
from app.services.retrieval import index_document, search_knowledge
from app.services.runtime import (
    _DEFINITION, _EMIT, audit, execute_agent, list_memories, references_of,
    request_control, resume_approval, run_eval, run_workflow,
)
from app.services.sandbox import run_sandbox
from app.services.vault import decrypt, encrypt
from app.util import dump, load
from app.api.schemas import *
from app.api.support import *

router = APIRouter()

def _case_dict(case: EvalCase) -> dict:
    return {
        "id": case.id,
        "dataset_id": case.dataset_id,
        "input_text": case.input_text,
        "expected": case.expected,
        "expected_tool": case.expected_tool or "",
        "created_at": stamp(case.created_at),
    }


def _dataset_dict(db, row: EvalDataset, ctx=None) -> dict:
    query = db.query(EvalRun).filter_by(dataset_id=row.id)
    if ctx is not None and not is_platform(ctx.user):
        query = query.filter(EvalRun.platform_only.is_(False), or_(EvalRun.target_space_id.is_(None), EvalRun.target_space_id == row.space_id))
    latest = query.order_by(EvalRun.id.desc()).first()
    agent = db.get(Agent, latest.agent_id) if latest and latest.agent_id else None
    version = db.get(AgentVersion, latest.version_id) if latest and latest.version_id else None
    model = db.get(ModelEndpoint, version.model_id) if version and version.model_id else None
    return {
        "id": row.id,
        "space_id": row.space_id,
        "space_name": db.get(Space, row.space_id).name if db.get(Space, row.space_id) else "",
        "name": row.name,
        "source_url": row.source_url or "",
        "license": row.license or "",
        "selection_note": row.selection_note or "",
        "default_metrics": load(row.default_metrics_json, ["contains", "completion"]),
        "case_count": db.query(EvalCase).filter_by(dataset_id=row.id).count(),
        "latest_agent": agent.name if agent else "",
        "latest_model": model.model if model else "",
        "latest_completion": latest.completion if latest else None,
        "created_at": stamp(row.created_at),
    }


def _load_dataset(db, ctx, dataset_id: int) -> EvalDataset:
    row = db.get(EvalDataset, dataset_id)
    if row is None or row.space_id not in ctx.space_ids:
        raise HTTPException(404, "评测集不存在")
    return row


def _eval_access(db, ctx, dataset: EvalDataset):
    membership = db.query(Membership).filter_by(user_id=ctx.user.id, space_id=dataset.space_id).first()
    role = "admin" if is_platform(ctx.user) else (membership.role if membership else "viewer")
    if RANK[role] < RANK["builder"]:
        raise HTTPException(403, "该空间需要构建者权限才能运行评测")
    space = db.get(Space, dataset.space_id)
    policy = db.query(ContextPolicy).filter_by(space_id=dataset.space_id).first()
    if space is None or policy is None:
        raise HTTPException(404, "评测集所属空间不存在")
    return space, policy, role


def _eval_target(db, ctx, dataset: EvalDataset, body: EvalRunIn):
    agent = db.get(Agent, body.agent_id) if body.agent_id else None
    workflow = db.get(Workflow, body.workflow_id) if body.workflow_id else None
    target = agent or workflow
    if target is None:
        raise HTTPException(404, "评测对象不存在")
    if not is_platform(ctx.user) and target.space_id != dataset.space_id:
        raise HTTPException(403, "只能评测数据集所属空间内的对象")
    if body.agent_version_id is not None:
        version = db.get(AgentVersion, body.agent_version_id)
        if agent is None or version is None or version.agent_id != agent.id:
            raise HTTPException(400, "所选智能体版本不属于评测对象")
    return agent, workflow


def _execution_context(db, ctx, dataset, target):
    space_id = target.space_id or dataset.space_id
    if space_id == dataset.space_id:
        return _eval_access(db, ctx, dataset)
    if not is_platform(ctx.user):
        raise HTTPException(403, "跨空间评测仅限平台管理员")
    space = db.get(Space, space_id)
    policy = db.query(ContextPolicy).filter_by(space_id=space_id).first()
    if space is None or policy is None:
        raise HTTPException(404, "评测对象所属空间不存在")
    return space, policy, "admin"


def _report_access(db, ctx, run_id: int, *, write: bool = False):
    row = db.get(EvalRun, run_id)
    dataset = db.get(EvalDataset, row.dataset_id) if row else None
    if dataset is None or dataset.space_id not in ctx.space_ids:
        raise HTTPException(404, "评测记录不存在")
    if not is_platform(ctx.user) and (row.platform_only or row.target_space_id not in (None, dataset.space_id)):
        raise HTTPException(404, "评测记录不存在")
    if write:
        _eval_access(db, ctx, dataset)
    return row, dataset


def _eval_run_dict(db, row: EvalRun, *, details: bool = False) -> dict:
    from app.services.evalkit import names
    agent = db.get(Agent, row.agent_id) if row.agent_id else None
    workflow = db.get(Workflow, row.workflow_id) if row.workflow_id else None
    version = db.get(AgentVersion, row.version_id) if row.version_id else None
    model = db.get(ModelEndpoint, version.model_id) if version and version.model_id else None
    cases = load(row.detail_json, [])
    chosen = load(row.metrics_json, [])
    result = {
        "id": row.id, "dataset_id": row.dataset_id, "workflow_revision": row.workflow_revision,
        "dataset_name": db.get(EvalDataset, row.dataset_id).name if db.get(EvalDataset, row.dataset_id) else "",
        "dataset_space_id": db.get(EvalDataset, row.dataset_id).space_id if db.get(EvalDataset, row.dataset_id) else None,
        "completion": row.completion, "quality": row.quality, "cost": row.cost, "passed": row.passed,
        "min_pass_rate": row.min_pass_rate or 1.0,
        "target_space_id": row.target_space_id,
        "platform_only": bool(row.platform_only),
        "note": row.note or "",
        "target_version": row.workflow_revision if workflow else (version.number if version else None),
        "created_at": stamp(row.created_at),
        "target": {"kind": "workflow" if workflow else "agent", "id": workflow.id if workflow else row.agent_id, "name": workflow.name if workflow else (agent.name if agent else "")},
        "agent": {"id": agent.id, "name": agent.name, "version": version.number if version else None, "model": model.name if model else "按任务选择", "model_code": model.model if model else ""} if agent else None,
        "metrics": [{"key": key, "name": names().get(key, key), "passed": sum(bool(item.get("scores", {}).get(key)) for item in cases), "total": len(cases)} for key in chosen],
    }
    if details:
        result["details"] = cases
    return result


@router.get("/api/evals/targets/{kind}/{target_id}/runs")
def target_eval_runs(kind: str, target_id: int, version: int | None = None, ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    if kind == "agent":
        target = db.get(Agent, target_id)
        if target is None or (not is_platform(ctx.user) and target.space_id not in ctx.space_ids):
            raise HTTPException(404, "智能体不存在")
        query = db.query(EvalRun).filter(EvalRun.agent_id == target_id)
        if version is not None:
            ids = [row.id for row in db.query(AgentVersion.id).filter_by(agent_id=target_id, number=version).all()]
            query = query.filter(EvalRun.version_id.in_(ids or [-1]))
    elif kind == "workflow":
        target = db.get(Workflow, target_id)
        if target is None or (not is_platform(ctx.user) and target.space_id not in ctx.space_ids):
            raise HTTPException(404, "工作流不存在")
        query = db.query(EvalRun).filter_by(workflow_id=target_id)
        if version is not None:
            query = query.filter_by(workflow_revision=version)
    else:
        raise HTTPException(400, "评测对象类型无效")
    if not is_platform(ctx.user):
        dataset_ids = db.query(EvalDataset.id).filter(EvalDataset.space_id.in_(ctx.space_ids))
        query = query.filter(EvalRun.dataset_id.in_(dataset_ids), EvalRun.platform_only.is_(False), or_(EvalRun.target_space_id.is_(None), EvalRun.target_space_id == target.space_id))
    return finish(query, lambda row: _eval_run_dict(db, row), desk, order=EvalRun.id.desc(), text=[], exact={}, time_col=EvalRun.created_at)


@router.get("/api/evals/datasets/{dataset_id}/runs")
def list_eval_runs(dataset_id: int, passed: bool | None = Query(default=None), ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    dataset = _load_dataset(db, ctx, dataset_id)
    query = db.query(EvalRun).filter_by(dataset_id=dataset_id)
    if not is_platform(ctx.user):
        query = query.filter(EvalRun.platform_only.is_(False), or_(EvalRun.target_space_id.is_(None), EvalRun.target_space_id == dataset.space_id))
    if passed is not None:
        query = query.filter(EvalRun.passed.is_(passed))
    query = query.outerjoin(Agent, Agent.id == EvalRun.agent_id).outerjoin(Workflow, Workflow.id == EvalRun.workflow_id)
    return finish(query, lambda row: _eval_run_dict(db, row), desk, order=EvalRun.id.desc(), text=[Agent.name, Workflow.name, cast(EvalRun.id, String), EvalRun.note], exact={"id": EvalRun.id, "agent_id": EvalRun.agent_id, "workflow_id": EvalRun.workflow_id, "workflow_revision": EvalRun.workflow_revision}, time_col=EvalRun.created_at)


@router.get("/api/evals/runs/{run_id}")
def get_eval_run(run_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    row, _ = _report_access(db, ctx, run_id)
    return _eval_run_dict(db, row, details=True)


@router.patch("/api/evals/runs/{run_id}")
def update_eval_run_note(run_id: int, body: EvalRunNoteIn, ctx=Depends(get_context), db=Depends(get_db)):
    row, dataset = _report_access(db, ctx, run_id, write=True)
    row.note = body.note.strip()
    audit(db, dataset.space_id, ctx.user.username, "eval.note", str(row.id))
    return _eval_run_dict(db, row)


def _drop_eval_run(db, ctx, run_id: int):
    row, dataset = _report_access(db, ctx, run_id, write=True)
    version_id = row.version_id if row.workflow_id is None else None
    db.delete(row)
    db.flush()
    if version_id:
        version = db.get(AgentVersion, version_id)
        if version:
            agent = db.get(Agent, version.agent_id)
            latest = db.query(EvalRun).filter_by(version_id=version_id, workflow_id=None).order_by(EvalRun.id.desc()).first()
            version.eval_passed = bool(latest.passed) if latest else bool(agent and agent.kind == "remote")
    audit(db, dataset.space_id, ctx.user.username, "eval.delete", str(run_id))


@router.delete("/api/evals/runs/{run_id}")
def delete_eval_run(run_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_eval_run(db, ctx, run_id)
    return {"ok": True}


@router.post("/api/evals/datasets/{dataset_id}/runs/bulk-delete")
def delete_eval_runs(dataset_id: int, body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    _eval_access(db, ctx, _load_dataset(db, ctx, dataset_id))
    for run_id in body.ids:
        row, _ = _report_access(db, ctx, run_id, write=True)
        if row.dataset_id != dataset_id:
            raise HTTPException(400, "评测记录不属于当前数据集")
        _drop_eval_run(db, ctx, run_id)
    return {"ok": True}


@router.get("/api/evals/datasets")
def list_datasets(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    query = db.query(EvalDataset).filter(EvalDataset.space_id.in_(ctx.space_ids))
    return finish(
        query,
        lambda row: _dataset_dict(db, row, ctx),
        desk,
        order=EvalDataset.id.desc(),
        text=[EvalDataset.name],
        exact={"name": EvalDataset.name},
        time_col=EvalDataset.created_at,
    )


@router.get("/api/evals/datasets/{dataset_id}")
def get_dataset(dataset_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    return _dataset_dict(db, _load_dataset(db, ctx, dataset_id), ctx)


@router.get("/api/evals/metrics")
def eval_metrics(ctx=Depends(get_context)):
    from app.services.evalkit import METRICS

    return METRICS


@router.get("/api/evals/sample.xlsx")
def eval_sample(ctx=Depends(get_context)):
    from app.services.sheet import workbook

    content = workbook(
        [
            ["输入", "期望", "期望工具"],
            ["计算 19*7", "133", "calculator"],
            ["退款规定是什么", "7天", "knowledge.search"],
        ]
    )
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=eval-sample.xlsx"},
    )


@router.post("/api/evals/parse")
async def eval_parse(file: UploadFile = File(...), ctx=Depends(get_context)):
    require(ctx, "builder")
    from app.services.sheet import cases_of

    name = (file.filename or "").lower()
    if name.endswith(".json"):
        try:
            payload = json.loads((await file.read()).decode("utf-8-sig"))
            raw = payload.get("cases") if isinstance(payload, dict) else None
            if not isinstance(raw, list) or not raw or len(raw) > 1000:
                raise ValueError("JSON 须包含 1–1000 条 cases")
            cases = [CaseIn.model_validate(item).model_dump() for item in raw]
            if any(not item["input_text"].strip() or not item["expected"].strip() for item in cases):
                raise ValueError("用例输入和期望答案不能为空")
            return {"cases": cases}
        except (ValueError, TypeError, UnicodeError) as exc:
            raise HTTPException(400, f"无法读取评测 JSON：{exc}") from exc
    if not name.endswith(".xlsx"):
        raise HTTPException(400, "请上传 xlsx 或 JSON 文件")
    try:
        cases = cases_of(await file.read())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "无法读取这个 Excel 文件") from exc
    return {"cases": cases}


@router.post("/api/evals/datasets")
def create_dataset(body: DatasetIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    from app.services.evalkit import KEYS
    if not body.default_metrics or any(item not in KEYS for item in body.default_metrics):
        raise HTTPException(400, "请选择有效的默认评测指标")
    row = EvalDataset(space_id=ctx.space.id, name=body.name, source_url=body.source_url or "", license=body.license or "", selection_note=body.selection_note or "", default_metrics_json=dump(body.default_metrics))
    db.add(row)
    db.flush()
    for item in body.cases:
        db.add(EvalCase(dataset_id=row.id, input_text=item.input_text, expected=item.expected, expected_tool=item.expected_tool))
    db.flush()
    return _dataset_dict(db, row, ctx)


@router.get("/api/evals/datasets/{dataset_id}/cases")
def list_cases(dataset_id: int, ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    row = _load_dataset(db, ctx, dataset_id)
    query = db.query(EvalCase).filter_by(dataset_id=row.id)
    return finish(
        query,
        _case_dict,
        desk,
        order=EvalCase.id.desc(),
        text=[EvalCase.input_text, EvalCase.expected, EvalCase.expected_tool],
        exact={"input_text": EvalCase.input_text, "expected": EvalCase.expected, "expected_tool": EvalCase.expected_tool},
        time_col=EvalCase.created_at,
    )


@router.post("/api/evals/datasets/{dataset_id}/cases/bulk-delete")
def delete_cases(dataset_id: int, body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    _eval_access(db, ctx, _load_dataset(db, ctx, dataset_id))
    return _bulk(db, body.ids, lambda item_id: _drop_case(db, ctx, item_id))


@router.post("/api/evals/datasets/{dataset_id}/cases")
def add_case(dataset_id: int, body: CaseIn, ctx=Depends(get_context), db=Depends(get_db)):
    row = _load_dataset(db, ctx, dataset_id)
    _eval_access(db, ctx, row)
    case = EvalCase(dataset_id=row.id, input_text=body.input_text, expected=body.expected, expected_tool=body.expected_tool)
    db.add(case)
    db.flush()
    return _case_dict(case)


@router.patch("/api/evals/datasets/{dataset_id}")
def patch_dataset(dataset_id: int, body: DatasetIn, ctx=Depends(get_context), db=Depends(get_db)):
    row = _load_dataset(db, ctx, dataset_id)
    _eval_access(db, ctx, row)
    row.name = body.name
    if body.source_url is not None:
        row.source_url = body.source_url
    if body.license is not None:
        row.license = body.license
    if body.selection_note is not None:
        row.selection_note = body.selection_note
    from app.services.evalkit import KEYS
    if not body.default_metrics or any(item not in KEYS for item in body.default_metrics):
        raise HTTPException(400, "请选择有效的默认评测指标")
    row.default_metrics_json = dump(body.default_metrics)
    return _dataset_dict(db, row, ctx)


@router.patch("/api/evals/cases/{case_id}")
def patch_case(case_id: int, body: CasePatch, ctx=Depends(get_context), db=Depends(get_db)):
    case = db.get(EvalCase, case_id)
    dataset = db.get(EvalDataset, case.dataset_id) if case else None
    if case is None or dataset is None or dataset.space_id not in ctx.space_ids:
        raise HTTPException(404, "用例不存在")
    _eval_access(db, ctx, dataset)
    if body.input_text is not None:
        case.input_text = body.input_text
    if body.expected is not None:
        case.expected = body.expected
    if body.expected_tool is not None:
        case.expected_tool = body.expected_tool
    return _case_dict(case)


def _drop_dataset(db, ctx, dataset_id: int) -> None:
    row = _load_dataset(db, ctx, dataset_id)
    _eval_access(db, ctx, row)
    affected_versions = {item[0] for item in db.query(EvalRun.version_id).filter_by(dataset_id=row.id).filter(EvalRun.workflow_id.is_(None), EvalRun.version_id.is_not(None)).all()}
    db.query(EvalCase).filter_by(dataset_id=row.id).delete()
    db.query(EvalRun).filter_by(dataset_id=row.id).delete()
    for version_id in affected_versions:
        version = db.get(AgentVersion, version_id)
        if version:
            latest = db.query(EvalRun).filter_by(version_id=version_id, workflow_id=None).order_by(EvalRun.id.desc()).first()
            agent = db.get(Agent, version.agent_id)
            version.eval_passed = bool(latest.passed) if latest else bool(agent and agent.kind == "remote")
    db.delete(row)


def _drop_case(db, ctx, case_id: int) -> None:
    case = db.get(EvalCase, case_id)
    dataset = db.get(EvalDataset, case.dataset_id) if case else None
    if case is None or dataset is None or dataset.space_id not in ctx.space_ids:
        raise HTTPException(404, "用例不存在")
    _eval_access(db, ctx, dataset)
    db.delete(case)


@router.delete("/api/evals/datasets/{dataset_id}")
def delete_dataset(dataset_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_dataset(db, ctx, dataset_id)
    return {"ok": True}


@router.post("/api/evals/datasets/bulk-delete")
def delete_datasets(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda item_id: _drop_dataset(db, ctx, item_id))


@router.delete("/api/evals/cases/{case_id}")
def delete_case(case_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop_case(db, ctx, case_id)
    return {"ok": True}


@router.post("/api/evals/datasets/{dataset_id}/run")
def eval_run(dataset_id: int, body: EvalRunIn, ctx=Depends(get_context), db=Depends(get_db)):
    from app.services.evalkit import KEYS

    dataset = _load_dataset(db, ctx, dataset_id)
    _eval_access(db, ctx, dataset)
    if bool(body.agent_id) == bool(body.workflow_id):
        raise HTTPException(400, "请选择一个智能体或一个工作流")
    unknown = [item for item in body.metrics if item not in KEYS]
    if unknown or not body.metrics:
        raise HTTPException(400, "请选择评测指标")
    agent, workflow = _eval_target(db, ctx, dataset, body)
    space, policy, role = _execution_context(db, ctx, dataset, agent or workflow)
    try:
        return run_eval(
            db,
            dataset,
            ctx.user,
            space,
            role,
            policy,
            agent=agent,
            workflow=workflow,
            agent_version_id=body.agent_version_id,
            metrics=body.metrics,
            min_pass_rate=body.min_pass_rate,
        )
    except GateError as exc:
        _raise_gate(exc, db, agent)


@router.post("/api/evals/datasets/{dataset_id}/run-stream")
def eval_run_stream(dataset_id: int, body: EvalRunIn, request: Request, ctx=Depends(get_context), db=Depends(get_db)):
    from app.services.evalkit import KEYS
    dataset = _load_dataset(db, ctx, dataset_id)
    _eval_access(db, ctx, dataset)
    if bool(body.agent_id) == bool(body.workflow_id):
        raise HTTPException(400, "请选择一个智能体或一个工作流")
    if not body.metrics or any(item not in KEYS for item in body.metrics):
        raise HTTPException(400, "请选择评测指标")
    agent, workflow = _eval_target(db, ctx, dataset, body)
    space, policy, role = _execution_context(db, ctx, dataset, agent or workflow)
    events = queue.Queue()
    factory = request.app.state.session_factory
    user_id, space_id = ctx.user.id, space.id

    def worker():
        session = factory()
        try:
            dataset = session.get(EvalDataset, dataset_id)
            user = session.get(User, user_id)
            space = session.get(Space, space_id)
            policy = session.query(ContextPolicy).filter_by(space_id=space_id).first()
            agent = session.get(Agent, body.agent_id) if body.agent_id else None
            workflow = session.get(Workflow, body.workflow_id) if body.workflow_id else None
            def progress(done, total, detail):
                events.put({"event": "progress", "done": done, "total": total, "passed": detail["passed"], "model_online": detail["model_online"]})
            result = run_eval(session, dataset, user, space, role, policy, agent=agent, workflow=workflow, agent_version_id=body.agent_version_id, metrics=body.metrics, min_pass_rate=body.min_pass_rate, progress=progress)
            session.commit()
            events.put({"event": "complete", "report": result})
        except Exception as exc:
            session.rollback()
            events.put({"event": "error", "message": str(exc)[:300]})
        finally:
            session.close()

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            try:
                item = events.get(timeout=15)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            yield f"data: {dump(item)}\n\n"
            if item["event"] in {"complete", "error"}:
                break

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

