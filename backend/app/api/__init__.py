"""HTTP 接口包。

按业务模块拆分路由，由这里汇成一个 APIRouter。
请求体在 schemas，跨模块的权限和加载规则在 support。
各路由文件只处理自己的资源，不再互相导入。
"""

from fastapi import APIRouter

from app.api.routers import (
    agents, approvals, audit, auth, chat, conversations, evals, knowledge, ledger, memories,
    models, org, sandbox, secrets, skills, system, tools, traces, workflows,
)

router = APIRouter()
for _module in (
    system, auth, org, models, tools, secrets, agents, ledger, knowledge,
    memories, skills, workflows, approvals, traces, evals, audit, sandbox, chat, conversations,
):
    router.include_router(_module.router)
