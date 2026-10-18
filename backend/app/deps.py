"""每个请求的数据库会话和当前操作者。

get_db 在请求成功时提交，异常时回滚。get_context 要求普通登录带 X-Space-Id；
委托令牌自己带 space_id，并且不允许用请求头换到别的空间。
角色从低到高是 viewer < operator < builder < admin，require 按这个顺序比较。
"""

from types import SimpleNamespace

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.models import ContextPolicy, Membership, Space, User
from app.services.grants import is_platform
from app.security import read_token

bearer = HTTPBearer(auto_error=False)
RANK = {"viewer": 1, "operator": 2, "builder": 3, "admin": 4}


def get_db(request: Request):
    db = request.app.state.session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db=Depends(get_db),
):
    if creds is None or not creds.credentials:
        raise HTTPException(401, "未登录")
    try:
        payload = read_token(creds.credentials)
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(401, "用户不存在")
    return SimpleNamespace(user=user, payload=payload)


def get_context(
    principal=Depends(get_principal),
    db=Depends(get_db),
    x_space_id: str | None = Header(default=None),
):
    payload = principal.payload
    if payload.get("typ") == "delegation":
        space_id = int(payload["space_id"])
        if x_space_id and int(x_space_id) != space_id:
            raise HTTPException(403, "委托令牌不能跨空间使用")
    else:
        if not x_space_id:
            raise HTTPException(400, "缺少空间")
        space_id = int(x_space_id)
    membership = (
        db.query(Membership).filter_by(user_id=principal.user.id, space_id=space_id).one_or_none()
    )
    space = db.get(Space, space_id)
    platform_admin = is_platform(principal.user)
    if membership is None and not platform_admin:
        raise HTTPException(403, "不在该空间")
    if space is None:
        raise HTTPException(404, "空间不存在")
    policy = db.query(ContextPolicy).filter_by(space_id=space_id).one_or_none()
    if policy is None:
        raise HTTPException(404, "空间没有上下文策略")
    return SimpleNamespace(
        user=principal.user,
        payload=payload,
        membership=membership,
        space=space,
        policy=policy,
        role=membership.role if membership is not None else "admin",
    )


def require(ctx, minimum: str) -> None:
    if RANK[ctx.role] < RANK[minimum]:
        raise HTTPException(403, "权限不足")
