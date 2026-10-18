"""知识入库和检索。

index_document 先按文档上的策略切块，再整篇替换旧块。检索按中文双字和英文词重合打分，
restricted 文档只对 allowed_roles 里的角色可见。这不是向量检索。
"""

import re

from sqlalchemy import or_

from app.models import Chunk, Document, KnowledgeBase
from app.services.ingest import split_text
from app.util import load


def index_document(db, document: Document) -> None:
    pieces, applied = split_text(
        document.content,
        document.strategy or "paragraph",
        document.chunk_size or 500,
        document.chunk_overlap or 0,
    )
    document.applied_strategy = applied
    document.chunk_count = len(pieces)
    db.query(Chunk).filter(Chunk.document_id == document.id).delete()
    for position, text in enumerate(pieces):
        db.add(Chunk(document_id=document.id, space_id=document.space_id, position=position, text=text))


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[\u4e00-\u9fff]{2}|\w+", (text or "").lower()))


def search_knowledge(db, space_id: int, query: str, role: str, base_id: int | None = None, base_ids: list[int] | None = None) -> list[dict]:
    q = (
        db.query(Chunk, Document)
        .join(Document, Document.id == Chunk.document_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.base_id)
    )
    if base_ids is not None:
        q = q.filter(Document.base_id.in_(base_ids or [-1]))
    else:
        q = q.filter(Chunk.space_id == space_id, KnowledgeBase.space_id == space_id)
    if base_id:
        q = q.filter(Document.base_id == base_id)
    found = []
    needles = _tokens(query)
    for chunk, document in q.all():
        if document.visibility == "restricted" and role not in load(document.allowed_roles_json, []):
            continue
        hay = _tokens(chunk.text)
        score = len(needles & hay) + sum(1 for token in needles if token and token in chunk.text.lower())
        if score <= 0:
            continue
        found.append(
            {
                "document_id": document.id,
                "title": document.title,
                "excerpt": chunk.text,
                "score": score,
            }
        )
    found.sort(key=lambda item: item["score"], reverse=True)
    return found[:3]


def visible_base_filter(space_id: int):
    return or_(KnowledgeBase.space_id == space_id)
