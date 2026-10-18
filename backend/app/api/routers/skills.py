"""部门技能的增删改查和压缩包导入。

部门能使用的技能来自授权。新建技能只授给当前部门。说明存在 skills 表，脚本文件由
skillbox.save_script 写到 backend/data/skills/{id}/。本模块不执行脚本；
执行发生在智能体循环收到 skill_script 函数调用之后。
"""

import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.api.schemas import IdsIn, SkillIn, SkillPatch
from app.api.support import _bulk
from app.deps import get_context, get_db, require
from app.models import Skill
from app.paging import Desk, desk_params, finish, stamp
from app.services.grants import drop_resource, ids_for, is_platform
from app.services.runtime import audit
from app.services.skillbox import read_script, remove_skill_dir, save_script, storage_path
from app.services.skillpack import parse_skill_zip
from app.util import dump, load

router = APIRouter()
_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _check_key(key: str) -> str:
    cleaned = (key or "").strip()
    if not _KEY.fullmatch(cleaned):
        raise HTTPException(400, "标识只能用小写字母、数字和短横线")
    return cleaned


def _skill_dict(row: Skill, ctx=None) -> dict:
    scripts = load(row.scripts_json, [])
    data = {
        "id": row.id,
        "space_id": row.space_id,
        "name": row.name,
        "key": row.key,
        "description": row.description,
        "body": row.body,
        "scripts": scripts,
        "enabled": row.enabled,
        "created_at": stamp(row.created_at),
        "owned": ctx is not None and row.space_id == ctx.space.id,
    }
    return data


def _readable(db, ctx, skill_id: int) -> Skill:
    row = db.get(Skill, skill_id)
    if row is None or row.id not in set(ids_for(db, ctx.space.id, "skill")):
        raise HTTPException(404, "技能不存在")
    return row


def _owned(db, ctx, skill_id: int) -> Skill:
    row = _readable(db, ctx, skill_id)
    if row.space_id != ctx.space.id:
        raise HTTPException(404, "技能不存在")
    return row


def _grant_new(db, ctx, row: Skill) -> None:
    from app.services.grants import grant

    grant(db, ctx.space.id, "skill", row.id)


def _sources(row: Skill) -> dict:
    """按清单读出每个脚本的正文。文件缺失时给空字符串，列表仍能显示文件名。"""
    found = {}
    for name in load(row.scripts_json, []):
        try:
            found[name] = read_script(row.id, name)
        except ValueError:
            found[name] = ""
    return found


@router.get("/api/skills")
def list_skills(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params), usable_space: int | None = Query(default=None)):
    space_id = ctx.space.id if usable_space is None else usable_space
    if usable_space is not None and not is_platform(ctx.user) and usable_space != ctx.space.id:
        raise HTTPException(403, "只能查看本部门已授权的资源")
    allowed = ids_for(db, space_id, "skill")
    query = db.query(Skill).filter(Skill.id.in_(allowed or [-1]))
    return finish(
        query,
        lambda row: _skill_dict(row, ctx),
        desk,
        order=Skill.id.desc(),
        text=[Skill.name, Skill.key, Skill.description],
        exact={"name": Skill.name, "key": Skill.key},
        time_col=Skill.created_at,
    )


@router.get("/api/skills/{skill_id}")
def get_skill(skill_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    """单条技能。script_sources 是文件名到正文，编辑时用来切换下拉框。"""
    row = _readable(db, ctx, skill_id)
    data = _skill_dict(row, ctx)
    data["script_sources"] = _sources(row)
    data["storage_path"] = storage_path(row.id)
    return data


@router.post("/api/skills")
def create_skill(body: SkillIn, ctx=Depends(get_context), db=Depends(get_db)):
    """手工新建。script_name 为空时只保存说明，不写脚本文件。"""
    require(ctx, "builder")
    key = _check_key(body.key)
    if db.query(Skill).filter_by(space_id=ctx.space.id, key=key).one_or_none():
        raise HTTPException(400, "技能标识已存在")
    row = Skill(
        space_id=ctx.space.id,
        name=body.name.strip()[:120] or "未命名技能",
        key=key,
        description=body.description,
        body=body.body,
        scripts_json="[]",
        enabled=body.enabled,
        created_by=ctx.user.id,
    )
    db.add(row)
    db.flush()
    if body.script_name.strip():
        try:
            name = save_script(row.id, body.script_name.strip(), body.script_source)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        row.scripts_json = dump([name])
    audit(db, ctx.space.id, ctx.user.username, "skill.create", row.key)
    _grant_new(db, ctx, row)
    return _skill_dict(row, ctx)


@router.post("/api/skills/import")
async def import_skill(file: UploadFile = File(...), ctx=Depends(get_context), db=Depends(get_db)):
    """上传 zip。名称、标识和正文都从包内 Markdown 读取，脚本清单以实际落下的文件为准。

    先写入数据库行拿到编号，再按编号存脚本。脚本名不合法时删掉已写的目录，
    请求失败会回滚这一行。
    """
    require(ctx, "builder")
    raw = await file.read()
    try:
        parsed = parse_skill_zip(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    key = parsed["key"]
    if db.query(Skill).filter_by(space_id=ctx.space.id, key=key).one_or_none():
        raise HTTPException(400, "技能标识已存在")
    row = Skill(
        space_id=ctx.space.id,
        name=parsed["name"],
        key=key,
        description=parsed["description"],
        body=parsed["body"],
        scripts_json="[]",
        enabled=True,
        created_by=ctx.user.id,
    )
    db.add(row)
    db.flush()
    names = []
    try:
        for filename, source in parsed["scripts"]:
            names.append(save_script(row.id, filename, source))
    except ValueError as exc:
        remove_skill_dir(row.id)
        raise HTTPException(400, str(exc)) from exc
    row.scripts_json = dump(names)
    audit(db, ctx.space.id, ctx.user.username, "skill.import", row.key)
    _grant_new(db, ctx, row)
    return _skill_dict(row, ctx)


@router.patch("/api/skills/{skill_id}")
def patch_skill(skill_id: int, body: SkillPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _owned(db, ctx, skill_id)
    if body.name is not None:
        row.name = body.name.strip()[:120] or row.name
    if body.description is not None:
        row.description = body.description
    if body.body is not None:
        row.body = body.body
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.script_files:
        known = load(row.scripts_json, [])
        for filename, source in body.script_files.items():
            if filename not in known:
                continue
            try:
                save_script(row.id, filename, source)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
    # 文件名和内容都有值才替换。编辑时留空表示保留已有脚本。
    elif body.script_name and body.script_name.strip() and (body.script_source or "").strip():
        try:
            name = save_script(row.id, body.script_name.strip(), body.script_source or "")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        scripts = [item for item in load(row.scripts_json, []) if item != name]
        scripts.append(name)
        row.scripts_json = dump(scripts[:8])
    audit(db, ctx.space.id, ctx.user.username, "skill.patch", row.key)
    return _skill_dict(row, ctx)


def _drop(db, ctx, skill_id: int) -> None:
    require(ctx, "builder")
    row = _owned(db, ctx, skill_id)
    remove_skill_dir(row.id)
    drop_resource(db, "skill", row.id)
    db.delete(row)


@router.delete("/api/skills/{skill_id}")
def delete_skill(skill_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    _drop(db, ctx, skill_id)
    return {"ok": True}


@router.post("/api/skills/bulk-delete")
def bulk_delete(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    return _bulk(db, body.ids, lambda skill_id: _drop(db, ctx, skill_id))
