"""应用装配。

启动时建表、给旧库补列，并在用户表为空时写入演示数据。
前端 dist 存在时，由同一进程托管单页应用；接口路径不会被前端路由吞掉。
SQLite 每个连接打开外键。会话 expire_on_commit 为假，提交后对象仍可读取。
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.api import router
from app.db import Base
from app.seed import ensure_builtin_tools, seed

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "frontend" / "dist"


class OperationLogMiddleware:
    """成功的写请求都留下操作日志。不记录请求体，避免把密钥写进审计。

    不用 BaseHTTPMiddleware。那一层会把流式响应收齐后再交给浏览器，问答就看不到逐段输出。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = {"code": 500}

        async def forward(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        await self.app(scope, receive, forward)
        path = scope.get("path") or ""
        method = scope.get("method") or "GET"
        if method not in {"POST", "PUT", "PATCH", "DELETE"} or status["code"] >= 400 or not path.startswith("/api"):
            return
        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers") or []}
        actor = ""
        header = headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            try:
                from app.security import read_token

                payload = read_token(header.split(" ", 1)[1])
                actor = str(payload.get("sub") or "")
            except ValueError:
                actor = ""
        space_header = headers.get("x-space-id") or ""
        db = scope["app"].state.session_factory()
        try:
            from app.models import User
            from app.services.runtime import audit

            name = actor
            if actor.isdigit():
                user = db.get(User, int(actor))
                name = user.username if user else actor
            space_id = int(space_header) if space_header.isdigit() else None
            audit(db, space_id, name or "anonymous", f"op.{method.lower()}", path, str(status["code"]))
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()


def create_app(database_url: str | None = None) -> FastAPI:
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        path = Path(__file__).resolve().parents[1] / "data" / "app.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
    if url.startswith("sqlite:///") and ":memory:" not in url:
        file_path = url.replace("sqlite:///", "", 1)
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    _ensure_columns(engine)
    _repair_agent_foreign_keys(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        from app.models import User

        if db.query(User).count() == 0:
            seed(db)
            db.commit()
        ensure_builtin_tools(db)
        db.commit()
        from app.services.grants import ensure_backfill, ensure_skill_grants

        ensure_backfill(db)
        ensure_skill_grants(db)
        from app.models import Membership

        platform_ids = [row[0] for row in db.query(User.id).filter(User.is_platform_admin.is_(True)).all()]
        if platform_ids:
            db.query(Membership).filter(Membership.user_id.in_(platform_ids)).delete(synchronize_session=False)
        db.commit()
        from app.models import Document
        from app.services.ingest import IngestError
        from app.services.retrieval import index_document

        pending = db.query(Document).filter((Document.chunk_count.is_(None)) | (Document.chunk_count == 0)).all()
        changed = False
        for row in pending:
            if not (row.content or "").strip():
                continue
            try:
                index_document(db, row)
                changed = True
            except IngestError:
                continue
        if changed:
            db.commit()
    finally:
        db.close()

    app = FastAPI(title="垣 · 智能体运行底座")
    app.state.session_factory = factory
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    from app.services.scheduler import start as start_scheduler

    start_scheduler(factory)
    from app.services.skillbox import start as start_skillbox
    from app.services.skillbox import stop as stop_skillbox

    # 启动时拉起技能容器，进程退出时删除。容器构建失败不阻止其余接口启动。
    app.add_event_handler("startup", start_skillbox)
    app.add_event_handler("shutdown", stop_skillbox)
    _mount_frontend(app)
    app.add_middleware(OperationLogMiddleware)
    return app


def _repair_agent_foreign_keys(engine) -> None:
    """旧库把智能体表改名后，外键仍指向 agents_department_old，新建版本会失败。"""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        broken = conn.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql LIKE '%agents_department_old%'"
        ).fetchall()
        if not broken:
            return
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        names = {row[0] for row in broken}
        if "agent_versions" in names:
            conn.exec_driver_sql(
                """
                CREATE TABLE agent_versions_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    agent_id INTEGER NOT NULL,
                    number INTEGER NOT NULL,
                    system_prompt TEXT NOT NULL,
                    tool_ids_json TEXT NOT NULL,
                    child_ids_json TEXT NOT NULL,
                    model_id INTEGER,
                    mcp_ids_json TEXT NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    eval_passed BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    skill_ids_json TEXT,
                    FOREIGN KEY(agent_id) REFERENCES agents (id)
                )
                """
            )
            conn.exec_driver_sql(
                """
                INSERT INTO agent_versions_new (
                    id, agent_id, number, system_prompt, tool_ids_json, child_ids_json,
                    model_id, mcp_ids_json, status, eval_passed, created_at, skill_ids_json
                )
                SELECT id, agent_id, number, system_prompt, tool_ids_json, child_ids_json,
                    model_id, mcp_ids_json, status, eval_passed, created_at, skill_ids_json
                FROM agent_versions
                """
            )
            conn.exec_driver_sql("DROP TABLE agent_versions")
            conn.exec_driver_sql("ALTER TABLE agent_versions_new RENAME TO agent_versions")
        if "tool_grants" in names:
            conn.exec_driver_sql(
                """
                CREATE TABLE tool_grants_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    agent_id INTEGER NOT NULL,
                    tool_id INTEGER NOT NULL,
                    CONSTRAINT uq_grant UNIQUE (agent_id, tool_id),
                    FOREIGN KEY(agent_id) REFERENCES agents (id),
                    FOREIGN KEY(tool_id) REFERENCES tools (id)
                )
                """
            )
            conn.exec_driver_sql(
                "INSERT INTO tool_grants_new (id, agent_id, tool_id) SELECT id, agent_id, tool_id FROM tool_grants"
            )
            conn.exec_driver_sql("DROP TABLE tool_grants")
            conn.exec_driver_sql("ALTER TABLE tool_grants_new RENAME TO tool_grants")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _ensure_columns(engine) -> None:
    """给已经存在的 SQLite 表补列。create_all 不会修改旧表。

    只做 ADD COLUMN，不删列、不改类型。新列的空值立刻填上默认值，
    避免后续代码把 NULL 当成缺失配置。
    """
    additions = {
        "users": [("is_platform_admin", "INTEGER")],
        "agents": [
            ("created_by", "INTEGER"),
            ("kind", "VARCHAR(20)"),
            ("remote_protocol", "VARCHAR(20)"),
            ("remote_endpoint", "VARCHAR(400)"),
            ("remote_secret_id", "INTEGER"),
            ("remote_timeout_ms", "INTEGER"),
            ("remote_health_url", "VARCHAR(400)"),
            ("remote_health_method", "VARCHAR(10)"),
        ],
        "memories": [("created_by", "INTEGER")],
        "memberships": [("created_at", "DATETIME")],
        "model_endpoints": [("created_at", "DATETIME"), ("quota_limit", "FLOAT"), ("quota_used", "FLOAT"), ("origin", "VARCHAR(20)"), ("created_by", "INTEGER")],
        "tools": [("created_at", "DATETIME"), ("origin", "VARCHAR(20)"), ("created_by", "INTEGER")],
        "knowledge_bases": [("created_at", "DATETIME"), ("origin", "VARCHAR(20)"), ("created_by", "INTEGER")],
        "documents": [
            ("created_at", "DATETIME"),
            ("source_name", "VARCHAR(240)"),
            ("source_kind", "VARCHAR(20)"),
            ("strategy", "VARCHAR(20)"),
            ("applied_strategy", "VARCHAR(20)"),
            ("chunk_size", "INTEGER"),
            ("chunk_overlap", "INTEGER"),
            ("chunk_count", "INTEGER"),
        ],
        "workflows": [
            ("created_at", "DATETIME"),
            ("enabled", "INTEGER"),
            ("origin", "VARCHAR(20)"),
            ("created_by", "INTEGER"),
            ("release", "VARCHAR(20)"),
            ("schedule_enabled", "INTEGER"),
            ("schedule_minutes", "INTEGER"),
            ("schedule_message", "TEXT"),
            ("schedule_nodes_json", "TEXT"),
            ("schedule_next_at", "DATETIME"),
            ("schedule_token_budget", "INTEGER"),
            ("schedule_token_used", "INTEGER"),
        ],
        "ledger_accounts": [("created_at", "DATETIME")],
        "eval_datasets": [("created_at", "DATETIME")],
        "eval_cases": [("created_at", "DATETIME"), ("expected_tool", "VARCHAR(80)")],
        "eval_runs": [("workflow_id", "INTEGER"), ("metrics_json", "TEXT")],
        "agent_versions": [("model_id", "INTEGER"), ("mcp_ids_json", "TEXT"), ("skill_ids_json", "TEXT")],
        "traces": [("dialogue_json", "TEXT")],
        "chat_sessions": [("pinned", "INTEGER")],
        "trace_steps": [
            ("tokens_in", "INTEGER"),
            ("tokens_out", "INTEGER"),
            ("cost_in", "FLOAT"),
            ("cost_out", "FLOAT"),
            ("secret_id", "INTEGER"),
        ],
    }
    text_defaults = {
        ("documents", "source_name"): "''",
        ("documents", "source_kind"): "'text'",
        ("documents", "strategy"): "'paragraph'",
        ("documents", "applied_strategy"): "'paragraph'",
        ("documents", "chunk_size"): "500",
        ("documents", "chunk_overlap"): "40",
        ("documents", "chunk_count"): "0",
        ("agent_versions", "mcp_ids_json"): "'[]'",
        ("agent_versions", "skill_ids_json"): "'[]'",
        ("workflows", "enabled"): "1",
        ("workflows", "release"): "'published'",
        ("workflows", "schedule_enabled"): "0",
        ("workflows", "schedule_minutes"): "0",
        ("workflows", "schedule_message"): "''",
        ("workflows", "schedule_nodes_json"): "'[]'",
        ("workflows", "schedule_token_budget"): "0",
        ("workflows", "schedule_token_used"): "0",
        ("model_endpoints", "quota_limit"): "0",
        ("model_endpoints", "quota_used"): "0",
        ("users", "is_platform_admin"): "0",
        ("model_endpoints", "origin"): "'department'",
        ("tools", "origin"): "'department'",
        ("knowledge_bases", "origin"): "'department'",
        ("workflows", "origin"): "'department'",
        ("trace_steps", "tokens_in"): "0",
        ("trace_steps", "tokens_out"): "0",
        ("trace_steps", "cost_in"): "0",
        ("trace_steps", "cost_out"): "0",
        ("eval_cases", "expected_tool"): "''",
        ("eval_runs", "metrics_json"): "'[]'",
        ("traces", "dialogue_json"): "'[]'",
        ("chat_sessions", "pinned"): "0",
        ("agents", "kind"): "'local'",
        ("agents", "remote_protocol"): "''",
        ("agents", "remote_endpoint"): "''",
        ("agents", "remote_timeout_ms"): "30000",
        ("agents", "remote_health_url"): "''",
        ("agents", "remote_health_method"): "'GET'",
    }
    with engine.begin() as conn:
        for table, columns in additions.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for column, kind in columns:
                if column not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                if column == "created_at":
                    conn.exec_driver_sql(f"UPDATE {table} SET {column} = CURRENT_TIMESTAMP WHERE {column} IS NULL")
                elif (table, column) in text_defaults:
                    conn.exec_driver_sql(
                        f"UPDATE {table} SET {column} = {text_defaults[(table, column)]} WHERE {column} IS NULL"
                    )
        conn.exec_driver_sql("UPDATE users SET is_platform_admin = 1 WHERE username = 'admin'")
        conn.exec_driver_sql("UPDATE model_endpoints SET origin = 'platform' WHERE space_id IS NULL")
        conn.exec_driver_sql("UPDATE tools SET origin = 'platform' WHERE space_id IS NULL")
        for table, action, column in (
            ("agents", "agent.create", "key"),
            ("model_endpoints", "model.create", "name"),
            ("tools", "tool.register", "name"),
        ):
            conn.exec_driver_sql(
                f"""
                UPDATE {table}
                SET created_by = (
                    SELECT users.id FROM audit_logs
                    JOIN users ON users.username = audit_logs.actor
                    WHERE audit_logs.action = '{action}' AND audit_logs.target = {table}.{column}
                    ORDER BY audit_logs.id ASC
                    LIMIT 1
                )
                WHERE created_by IS NULL
                """
            )
        conn.exec_driver_sql("UPDATE memories SET created_by = user_id WHERE created_by IS NULL AND user_id IS NOT NULL")
        conn.exec_driver_sql(
            "UPDATE agents SET created_by = (SELECT id FROM users WHERE username = 'admin') WHERE created_by IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE model_endpoints SET created_by = (SELECT id FROM users WHERE username = 'admin') WHERE created_by IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE tools SET created_by = (SELECT id FROM users WHERE username = 'admin') WHERE created_by IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE knowledge_bases SET created_by = (SELECT id FROM users WHERE username = 'admin') WHERE created_by IS NULL AND name = '客服制度'"
        )
        conn.exec_driver_sql(
            "UPDATE workflows SET created_by = (SELECT id FROM users WHERE username = 'admin') WHERE created_by IS NULL AND name = '问答与计算'"
        )
    _relax_agent_department(engine)


def _relax_agent_department(engine) -> None:
    """旧库里智能体部门是必填。改成可空，平台管理员才能保存未分配部门的智能体。"""
    with engine.begin() as conn:
        rows = list(conn.exec_driver_sql("PRAGMA table_info(agents)"))
        space = next((row for row in rows if row[1] == "space_id"), None)
        if space is None or space[3] == 0:
            return
        columns = [row[1] for row in rows]
        joined = ", ".join(columns)
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("ALTER TABLE agents RENAME TO agents_department_old")
        from app.models import Agent

        Agent.__table__.create(conn)
        conn.exec_driver_sql(f"INSERT INTO agents ({joined}) SELECT {joined} FROM agents_department_old")
        conn.exec_driver_sql("DROP TABLE agents_department_old")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _mount_frontend(app: FastAPI) -> None:
    index = DIST / "index.html"
    if not index.exists():
        return

    @app.get("/", include_in_schema=False)
    def spa_index():
        return FileResponse(index)

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith(("api", "v1", "docs", "openapi", "redoc")):
            raise HTTPException(404, "未找到")
        target = (DIST / full_path).resolve()
        if not str(target).startswith(str(DIST.resolve())):
            raise HTTPException(404, "未找到")
        if target.is_file():
            return FileResponse(target)
        return FileResponse(index)
