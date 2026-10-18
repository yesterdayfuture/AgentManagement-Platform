"""评测集、用例和运行。用例可以逐条维护，也可以从表格解析。"""

import queue
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.deps import get_context, get_db, get_principal, require
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


def _dataset_dict(db, row: EvalDataset) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "case_count": db.query(EvalCase).filter_by(dataset_id=row.id).count(),
        "created_at": stamp(row.created_at),
    }


def _load_dataset(db, ctx, dataset_id: int) -> EvalDataset:
    row = db.get(EvalDataset, dataset_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "评测集不存在")
    return row


@router.get("/api/evals/datasets")
def list_datasets(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    query = db.query(EvalDataset).filter_by(space_id=ctx.space.id)
    return finish(
        query,
        lambda row: _dataset_dict(db, row),
        desk,
        order=EvalDataset.id.desc(),
        text=[EvalDataset.name],
        exact={"name": EvalDataset.name},
        time_col=EvalDataset.created_at,
    )


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
    if not name.endswith(".xlsx"):
        raise HTTPException(400, "请上传 xlsx 文件")
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
    row = EvalDataset(space_id=ctx.space.id, name=body.name)
    db.add(row)
    db.flush()
    for item in body.cases:
        db.add(EvalCase(dataset_id=row.id, input_text=item.input_text, expected=item.expected, expected_tool=item.expected_tool))
    db.flush()
    return _dataset_dict(db, row)


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
    _load_dataset(db, ctx, dataset_id)
    return _bulk(db, body.ids, lambda item_id: _drop_case(db, ctx, item_id))


@router.post("/api/evals/datasets/{dataset_id}/cases")
def add_case(dataset_id: int, body: CaseIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = db.get(EvalDataset, dataset_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "评测集不存在")
    case = EvalCase(dataset_id=row.id, input_text=body.input_text, expected=body.expected, expected_tool=body.expected_tool)
    db.add(case)
    db.flush()
    return _case_dict(case)


@router.patch("/api/evals/datasets/{dataset_id}")
def patch_dataset(dataset_id: int, body: DatasetIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = db.get(EvalDataset, dataset_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "评测集不存在")
    row.name = body.name
    return _dataset_dict(db, row)


@router.patch("/api/evals/cases/{case_id}")
def patch_case(case_id: int, body: CasePatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    case = db.get(EvalCase, case_id)
    dataset = db.get(EvalDataset, case.dataset_id) if case else None
    if case is None or dataset is None or dataset.space_id != ctx.space.id:
        raise HTTPException(404, "用例不存在")
    if body.input_text is not None:
        case.input_text = body.input_text
    if body.expected is not None:
        case.expected = body.expected
    if body.expected_tool is not None:
        case.expected_tool = body.expected_tool
    return _case_dict(case)


def _drop_dataset(db, ctx, dataset_id: int) -> None:
    require(ctx, "builder")
    row = db.get(EvalDataset, dataset_id)
    if row is None or row.space_id != ctx.space.id:
        raise HTTPException(404, "评测集不存在")
    db.query(EvalCase).filter_by(dataset_id=row.id).delete()
    db.query(EvalRun).filter_by(dataset_id=row.id).delete()
    db.delete(row)


def _drop_case(db, ctx, case_id: int) -> None:
    require(ctx, "builder")
    case = db.get(EvalCase, case_id)
    dataset = db.get(EvalDataset, case.dataset_id) if case else None
    if case is None or dataset is None or dataset.space_id != ctx.space.id:
        raise HTTPException(404, "用例不存在")
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
    require(ctx, "builder")
    from app.services.evalkit import KEYS

    dataset = _load_dataset(db, ctx, dataset_id)
    if bool(body.agent_id) == bool(body.workflow_id):
        raise HTTPException(400, "请选择一个智能体或一个工作流")
    unknown = [item for item in body.metrics if item not in KEYS]
    if unknown or not body.metrics:
        raise HTTPException(400, "请选择评测指标")
    agent = _load_agent(db, ctx, body.agent_id) if body.agent_id else None
    workflow = _load_workflow(db, ctx, body.workflow_id) if body.workflow_id else None
    try:
        return run_eval(
            db,
            dataset,
            ctx.user,
            ctx.space,
            ctx.role,
            ctx.policy,
            agent=agent,
            workflow=workflow,
            metrics=body.metrics,
        )
    except GateError as exc:
        _raise_gate(exc, db, agent)

