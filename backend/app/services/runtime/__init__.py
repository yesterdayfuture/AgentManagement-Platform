"""运行时门面。

外部和测试只依赖这个模块上的名字。
工具循环内部也从这里读取模型调用、轮次上限、工具目录和退避等待，
因此替换这些属性后，下一次执行会走替换后的实现。
"""

from app.services.gateway import complete_with_tools
from app.services.runtime.support import _DEFINITION, _EMIT, audit, list_memories, references_of
from app.services.runtime.agent import _LOOP_TURNS, _pause, _tool_catalog, execute_agent, resume_approval
from app.services.runtime.workflow import request_control, run_workflow
from app.services.runtime.evaluation import run_eval

__all__ = [
    "complete_with_tools",
    "_DEFINITION",
    "_EMIT",
    "_LOOP_TURNS",
    "_pause",
    "_tool_catalog",
    "audit",
    "execute_agent",
    "list_memories",
    "references_of",
    "request_control",
    "resume_approval",
    "run_eval",
    "run_workflow",
]
