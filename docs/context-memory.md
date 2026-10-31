# 长对话压缩与三层记忆

问答、智能体执行、工作流执行共用同一套上下文压缩与记忆生产逻辑（分层思路参考 Three_momery：短期 / 长期 / 用户画像）。

相关代码：

| 模块 | 路径 |
|---|---|
| 分层压缩 | `backend/app/services/context_compress.py` |
| 压缩编排 + 注入 | `backend/app/services/compress_runtime.py` |
| 三层记忆生产 / 可见性 | `backend/app/services/memory_layers.py` |
| 问答主动压缩 | `POST /api/chats/{id}/compress`（`conversations.py`） |
| 记忆管理 API | `backend/app/api/routers/memories.py` |
| 控制台 | `frontend/src/views/Ask.vue`、`Memories.vue` |

## 压缩策略

### 触发条件

- **自动**：送入模型的消息估算 token 达到该模型 `context_tokens` 的 **80%**（问答历史、智能体 `prior` / 工具循环、工作流内各步共用）。
- **主动**：问答页「压缩上下文」→ `POST /api/chats/{session_id}/compress`（`force=True`，不依赖是否超预算）。

Token 估算为粗略长度启发（约 4 字符 ≈ 1 token），预算取 `max(1024, context_tokens * 0.8)`。模型接入点字段 `context_tokens` 默认 `1000000`（新建不填时同此值），可在模型管理中配置。

### 分层处理

1. **轻量剪枝**（`light_prune`）：按重要性打分，优先保留 system、已有摘要、首尾轮、结构化内容；丢掉中间低分消息。
2. **重量级摘要**：剪枝后仍超预算（或主动压缩）时，折叠可压缩段，调用推理模型生成摘要；写入 `kind=summary` 记忆，并触发三类记忆抽取。

### 重要性与绕过

高分应保留的信号包括：system / summary 角色、对话首尾、代码块与数学公式、工具/错误/结论等关键词、敏感词停机说明等。

结构化内容（Markdown 代码围栏、`$...$` / `$$...$$`、LaTeX `begin/end` 等）**自动绕过**压缩折叠，避免代码与公式被摘要冲掉。

### 各场景的 `task_id` / 来源

| 场景 | `source_kind` | `source_name` | `task_id`（短期作用域） |
|---|---|---|---|
| 模型问答 | `chat` | 会话标题 | 会话 `id` |
| 智能体试运行 | `agent` | 智能体名称 | 调用方传入，或 `resume_trace_id`，否则本次 `trace.id` |
| 工作流执行 | `workflow` | 工作流名称 | `run-{run.id}`（同一次运行内各节点共享） |

主动压缩成功且产生摘要时，会在会话中追加一条 `role=summary` 的消息，便于界面展示；完整消息历史仍保留。

## 三层记忆

| 种类 | kind | 可见与使用范围 | 过期 |
|---|---|---|---|
| 短期 | `short` | 仅当前问答会话，或当前智能体 / 工作流**本次任务**（同 `task_id`） | 较短（约保留天数上限 14 天） |
| 长期 | `long` | 当前用户在问答、智能体、工作流中均可注入 | 较长（至少约 90 天或策略保留天数） |
| 用户画像 | `user` | 用户只能看自己的；平台管理员可在记忆管理中查看各用户 | 默认不过期 |
| 摘要 | `summary` | 压缩产物；详情含溯源；运行时不注入提示词 | 不过期 |

来源标注：`source_kind`、`source_id`、`source_name`、`task_id`。

### 摘要时如何生产三类记忆

触发重量级摘要后，`extract_and_store` 结合**历史摘要 + 本轮折叠对话**调用模型抽取 JSON：

- `short_term` → 短期
- `long_term` → 长期
- `user_memory` → 用户画像

并写入摘要行的：

- `source_refs_json`：依据的对话片段与历史摘要
- `produced_json`：产出的 short / long / user 记忆 id 列表

若模型未抽出短期，会用本轮摘要正文兜底一条短期。

### 任务结束后如何落库

问答一轮、智能体一次运行、工作流一次运行在**成功结束**后都会调用 `capture_task_memories`：

1. 优先用模型从本轮对话抽取 short / long / user
2. 模型抽不出 JSON 时用兜底规则：至少写一条短期（输入→输出）；识别「我喜欢 / 请叫我」等写入用户记忆，「记得 / 默认」等写入长期
3. 写入 `source_kind`（`chat` / `agent` / `workflow`）与 `source_name`（会话标题、智能体名、工作流名），便于记忆管理过滤

压缩到 80% 或主动压缩时仍走 `extract_and_store`，额外写入 `summary` 溯源行。

### 运行时注入

- **工具循环**（连得上的模型）：`inject_memories` 把可见的短期 / 长期 / 用户写入 system 提示；短期优先排序。
- **规则对话路径**（`_chat`）：长期与用户进入 memories 列表（规则模型可回显）；短期只写入系统提示，避免同工作流任务内上一步输入覆盖下一步回复。

摘要（`summary`）本身不注入，只作为压缩后的对话角色或记忆管理中的溯源记录。

## 记忆管理界面

路径：`/memories`。

- 过滤：来源（问答 / 智能体 / 工作流）、类型（短 / 长 / 用户 / 摘要）、会话或任务名关键词
- **查看**摘要：展示溯源对话、历史摘要，以及产出的长期 / 用户 / 短期记忆
- 权限：普通用户看自己的；平台管理员可看各部门与各用户的用户记忆

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/chats/{id}/compress` | 主动压缩；返回会话消息与 `compress` 统计 |
| `GET` | `/api/memories` | 列表；查询参数 `kind`、`source_kind`、`source_name`、`task_id`、`agent_id` 及 Desk 分页字段 |
| `GET` | `/api/memories/{id}` | 详情；`detail.source_refs`、`detail.produced` |
| `POST` | `/api/memories` | 手动创建 `short` / `long` / `user` |
| `PATCH` / `DELETE` | `/api/memories/{id}` | 修改 / 删除（不能改他人的，除非管理员） |
| `GET` / `POST` | `/api/memories/export`、`/import` | 导出 / 导入（不含密钥；导出不含摘要） |

模型：

- 创建 / 更新时可设 `context_tokens`（`POST/PATCH /api/models`）

## 测试

`backend/tests/test_platform.py`：

| 用例 | 覆盖 |
|---|---|
| `test_task_runs_produce_sourced_memories` | 问答 / 智能体 / 工作流执行后出现带来源的记忆 |
| `test_short_term_memory_scoped_to_task` | **短期**：API 创建与过滤；同 `task_id` 可见/注入，跨任务不可见 |
| `test_long_term_memory_cross_task_and_user_isolation` | **长期**：跨任务注入智能体；其他用户不可见；本人可改、他人不可改 |
| `test_user_memory_owner_only_and_platform_admin` | **用户**：本人可见可注入；他人 404；平台管理员可列表；不过期 |
| `test_three_tier_memory_api_and_visibility` | 三类一并创建与按 `kind` 过滤冒烟 |
| `test_three_tier_memory_extract_and_injection` | 摘要抽取同时产出三类；短期按任务隔离；长期/用户注入；摘要详情溯源 |
| `test_memory_isolation_export_and_expiry` | 长期隔离、导出、TTL 过期 |

本地跑相关用例：

```bash
cd backend
.venv/bin/python -m pytest tests/test_platform.py -k "memory" -q
```
