"""执行结果质量评价。评价附着在轨迹上，删除评价不删除原始执行。"""

import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.deps import get_context, get_db
from app.models import Agent, AgentVersion, Space, Trace, User, Workflow, WorkflowRun
from app.paging import Desk, desk_params, finish, stamp
from app.services.grants import is_platform

router = APIRouter()


class QualityIn(BaseModel):
    score: int = Field(ge=1, le=5)
    note: str = Field(default="", max_length=2000)


def _admin(ctx):
    if not is_platform(ctx.user):
        raise HTTPException(403, "仅平台管理员可查看质量评价")


def _item(db, row: Trace):
    agent = db.get(Agent, row.agent_id) if row.agent_id else None
    workflow = db.get(Workflow, row.workflow_id) if row.workflow_id else None
    space = db.get(Space, row.space_id)
    reviewer = db.get(User, row.quality_reviewer_id) if row.quality_reviewer_id else None
    agent_version = db.get(AgentVersion, row.version_id) if row.version_id else None
    workflow_run = db.query(WorkflowRun).filter_by(trace_id=row.id).order_by(WorkflowRun.id.desc()).first() if row.workflow_id else None
    clean_output = re.sub(r"^【v\d+】\s*", "", row.output_text or "")
    return {
        "id": row.id, "space_id": row.space_id, "department": space.name if space else "",
        "agent_id": row.agent_id, "agent_name": agent.name if agent else "",
        "workflow_id": row.workflow_id, "workflow_name": workflow.name if workflow else "",
        "input": row.input_text, "output": clean_output, "status": row.status,
        "agent_version": agent_version.number if agent_version else None,
        "agent_version_status": agent_version.status if agent_version else None,
        "workflow_version": workflow_run.workflow_revision if workflow_run else None,
        "score": row.quality_score, "note": row.quality_note or "",
        "reviewer": reviewer.display_name or reviewer.username if reviewer else "",
        "created_at": stamp(row.created_at), "reviewed_at": stamp(row.quality_reviewed_at),
    }


@router.get("/api/quality-reviews")
def list_reviews(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params), space_id: int = 0, agent_id: int = 0, workflow_id: int = 0):
    _admin(ctx)
    query = db.query(Trace).filter(Trace.quality_score.is_not(None))
    if space_id:
        query = query.filter(Trace.space_id == space_id)
    if agent_id:
        query = query.filter(Trace.agent_id == agent_id)
    if workflow_id:
        query = query.filter(Trace.workflow_id == workflow_id)
    query = query.outerjoin(Agent, Agent.id == Trace.agent_id).outerjoin(Workflow, Workflow.id == Trace.workflow_id)
    return finish(query, lambda row: _item(db, row), desk, order=Trace.quality_reviewed_at.desc(), text=[Trace.input_text, Trace.output_text, Trace.quality_note, Agent.name, Workflow.name], exact={"score": Trace.quality_score, "status": Trace.status}, time_col=Trace.created_at)


@router.get("/api/quality-reviews/export")
def export_reviews(purpose: str = Query(pattern="^(finetune|evaluation)$"), ids: list[str] = Query(default=[]), ctx=Depends(get_context), db=Depends(get_db)):
    _admin(ctx)
    if not ids:
        raise HTTPException(400, "请先选择要导出的评价")
    if len(ids) > 1000 or len(set(ids)) != len(ids):
        raise HTTPException(400, "导出编号无效或超过 1000 条")
    found = db.query(Trace).filter(Trace.id.in_(ids), Trace.quality_score.is_not(None)).all()
    if len(found) != len(ids):
        raise HTTPException(404, "所选评价包含不存在的记录")
    by_id = {row.id: _item(db, row) for row in found}
    rows = [by_id[item_id] for item_id in ids]
    bad = [row["id"] for row in rows if row["score"] < 4 or row["status"] != "completed" or not row["input"].strip() or not row["output"].strip()]
    if bad:
        raise HTTPException(422, f"训练和评估数据仅接受已完成、评分至少 4 分且输入输出非空的记录；不符合：{', '.join(bad[:5])}")
    if purpose == "finetune":
        body = "\n".join(json.dumps({"messages": [{"role": "user", "content": row["input"]}, {"role": "assistant", "content": row["output"]}]}, ensure_ascii=False) for row in rows) + "\n"
        filename = "quality-finetune.jsonl"
        media_type = "application/x-ndjson; charset=utf-8"
    else:
        body = json.dumps({"name": "质量评价导出评估集", "default_metrics": ["contains", "completion"], "selection_note": "人工评分至少 4/5；模型回复作为参考答案，导入前请人工复核事实正确性。", "cases": [{"input_text": row["input"], "expected": row["output"], "expected_tool": "", "quality_score": row["score"], "trace_id": row["id"], "agent_id": row["agent_id"], "agent_version": row["agent_version"], "workflow_id": row["workflow_id"], "workflow_version": row["workflow_version"]} for row in rows]}, ensure_ascii=False, indent=2)
        filename = "quality-evaluation.json"
        media_type = "application/json; charset=utf-8"
    return Response(body, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/api/traces/{trace_id}/quality")
def create_review(trace_id: str, body: QualityIn, ctx=Depends(get_context), db=Depends(get_db)):
    row = db.get(Trace, trace_id)
    if row is None or (not is_platform(ctx.user) and row.space_id not in ctx.space_ids):
        raise HTTPException(404, "执行记录不存在")
    if row.status in {"running", "waiting", "paused"}:
        raise HTTPException(409, "执行尚未结束，不能评价")
    if row.quality_score is not None:
        raise HTTPException(409, "已有质量评价，请编辑")
    row.quality_score = body.score
    row.quality_note = body.note.strip()
    row.quality_reviewer_id = ctx.user.id
    row.quality_reviewed_at = datetime.utcnow()
    return _item(db, row)


@router.get("/api/quality-reviews/{trace_id}")
def get_review(trace_id: str, ctx=Depends(get_context), db=Depends(get_db)):
    _admin(ctx)
    row = db.get(Trace, trace_id)
    if row is None or row.quality_score is None:
        raise HTTPException(404, "质量评价不存在")
    return _item(db, row)


@router.patch("/api/quality-reviews/{trace_id}")
def update_review(trace_id: str, body: QualityIn, ctx=Depends(get_context), db=Depends(get_db)):
    row = db.get(Trace, trace_id)
    if row is None or row.quality_score is None or (not is_platform(ctx.user) and (row.space_id not in ctx.space_ids or row.quality_reviewer_id != ctx.user.id)):
        raise HTTPException(404, "质量评价不存在")
    row.quality_score = body.score
    row.quality_note = body.note.strip()
    row.quality_reviewer_id = ctx.user.id
    row.quality_reviewed_at = datetime.utcnow()
    return _item(db, row)


@router.delete("/api/quality-reviews/{trace_id}")
def delete_review(trace_id: str, ctx=Depends(get_context), db=Depends(get_db)):
    _admin(ctx)
    row = db.get(Trace, trace_id)
    if row is None or row.quality_score is None:
        raise HTTPException(404, "质量评价不存在")
    row.quality_score = None
    row.quality_note = ""
    row.quality_reviewer_id = None
    row.quality_reviewed_at = None
    return {"ok": True}
