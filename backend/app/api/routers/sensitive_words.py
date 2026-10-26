"""敏感词清单：按部门隔离，一条记录可含多个词。"""

from fastapi import APIRouter, Depends, HTTPException

from app.api.schemas import IdsIn, SensitiveWordIn, SensitiveWordPatch
from app.api.support import _attribution, _bulk, _place
from app.deps import get_context, get_db, require
from app.models import SensitiveWordList
from app.paging import Desk, desk_params, finish, stamp
from app.services.grants import is_platform
from app.services.sensitive import parse_words
from app.util import dump, load

router = APIRouter()


def _words_of(body_words, body_text: str | None = None) -> list[str]:
    if body_words:
        return parse_words(body_words)
    return parse_words(body_text or "")


def _dict(db, row: SensitiveWordList) -> dict:
    words = parse_words(load(row.words_json, []))
    return {
        "id": row.id,
        "name": row.name,
        "words": words,
        "words_text": "、".join(words),
        "word_count": len(words),
        "enabled": bool(row.enabled),
        "space_id": row.space_id,
        "created_at": stamp(row.created_at),
        **_attribution(db, row.created_by, row.space_id),
    }


def _load(db, ctx, item_id: int) -> SensitiveWordList:
    row = db.get(SensitiveWordList, item_id)
    if row is None or (not is_platform(ctx.user) and row.space_id != ctx.space.id):
        raise HTTPException(404, "敏感词清单不存在")
    return row


@router.get("/api/sensitive-words")
def list_words(ctx=Depends(get_context), db=Depends(get_db), desk: Desk = Depends(desk_params)):
    require(ctx, "viewer")
    query = db.query(SensitiveWordList)
    if is_platform(ctx.user):
        pass
    else:
        query = query.filter(SensitiveWordList.space_id == ctx.space.id)
    return finish(
        query,
        lambda row: _dict(db, row),
        desk,
        order=SensitiveWordList.id.desc(),
        text=[SensitiveWordList.name, SensitiveWordList.words_json],
        exact={"name": SensitiveWordList.name},
        time_col=SensitiveWordList.created_at,
    )


@router.post("/api/sensitive-words")
def create_words(body: SensitiveWordIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "请填写名称")
    words = _words_of(body.words, body.words_text)
    if not words:
        raise HTTPException(400, "请至少填写一个敏感词")
    row = SensitiveWordList(
        space_id=_place(db, ctx, body.space_id),
        name=name,
        words_json=dump(words),
        enabled=bool(body.enabled),
        created_by=ctx.user.id,
    )
    db.add(row)
    db.flush()
    return _dict(db, row)


@router.patch("/api/sensitive-words/{item_id}")
def patch_words(item_id: int, body: SensitiveWordPatch, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load(db, ctx, item_id)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "请填写名称")
        row.name = name
    if body.words is not None or body.words_text is not None:
        words = _words_of(body.words, body.words_text)
        if not words:
            raise HTTPException(400, "请至少填写一个敏感词")
        row.words_json = dump(words)
    if body.enabled is not None:
        row.enabled = bool(body.enabled)
    db.flush()
    return _dict(db, row)


@router.delete("/api/sensitive-words/{item_id}")
def delete_words(item_id: int, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")
    row = _load(db, ctx, item_id)
    db.delete(row)
    return {"ok": True}


@router.post("/api/sensitive-words/bulk-delete")
def bulk_delete(body: IdsIn, ctx=Depends(get_context), db=Depends(get_db)):
    require(ctx, "builder")

    def drop(item_id: int) -> None:
        row = _load(db, ctx, item_id)
        db.delete(row)

    return _bulk(db, body.ids, drop)
