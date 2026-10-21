# LIMEN (垣)

[English](README.en.md) · [中文](README.md)

LIMEN is an on-prem agent runtime. Models handle understanding and decisions; tools, retrieval, ledgers, and skill scripts run inside a controlled runtime. Every call leaves a trace, steps, and cost. High-risk writes pause until approval, then commit.

Departments are isolation boundaries. Models, workflows, knowledge bases, tools, and skills are usable only after they are granted to that department. Unauthorized entries never appear in the console.

![Overview. The skill container refreshes every second and summarizes calls, cost, and tokens.](docs/images/overview.png)

The skill container `limen-skillbox` starts with the API process and is destroyed when the process exits. Scripts run only inside that container. Overview polls container status and in-flight / recently finished script jobs every second.

## Stack

| Part | Location | Notes |
|---|---|---|
| Console | `frontend/` | Vue 3 + Vite. Build output in `frontend/dist`, served by the same backend process. |
| API | `backend/app/` | FastAPI. Entry is `app` in `backend/app/main.py`. |
| Data | SQLite + SQLAlchemy | Default DB file `backend/data/app.db`. Override with `DATABASE_URL`. |
| Tests | `backend/tests/test_platform.py` | In-memory DB; does not touch the dev database. |

Dependencies: `backend/requirements.txt` and `frontend/package.json`.

## Layout

```
backend/app/main.py                         Process entry; calls create_app
backend/app/core.py                         Schema, migrations, seed, static frontend
backend/app/api/                            HTTP routers by domain
backend/app/api/routers/conversations.py    Chat sessions
backend/app/models.py                       Tables
backend/app/seed.py                         Demo data for empty DBs; backfill for older DBs
backend/app/services/runtime/               Agent loop, workflows, evals
backend/app/services/gateway.py             Intent classify and tool gate
backend/app/services/builtins.py            Built-in tool parameter docs
frontend/src/views/Ask.vue                  Chat page
frontend/src/views/TraceDetail.vue          Single-run trace
frontend/src/App.vue                        Left nav
docs/builtin-tools.md                       How to add a built-in tool
docs/skills.md                              Skills, zip packs, grants, container exec
docs/ask-workflow.md                        Ask → draft workflow prompts and model input
docs/autonomous-agent.md                    Autonomous scheduling workflows
docs/remote-async-agent.md                  Build a remote async agent with Python + FastAPI
docs/images/                                Console screenshots
README.md / README.en.md                    Chinese / English docs (switch links at top)
```

## Local start

Day-to-day: API on `:8000`, Vite console on `:5173` (HMR; no rebuild required each change).

```bash
# Terminal 1: API
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Terminal 2: console
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. Restart uvicorn after Python changes.

Alternatively run `npm run build` then only the backend (e.g. `:8026`); if `frontend/dist` exists, the same process serves the UI. Vue source changes then need a rebuild.

With no `DATABASE_URL`, the DB file is `backend/data/app.db`. When the user table is empty, `seed()` writes demo data. Every start runs `ensure_builtin_tools()` to backfill missing global built-ins. Demo accounts exist only in empty-DB seed; existing DBs do not reset passwords on start.

## Demo accounts

After first start on an empty DB:

| Username | Password | Role |
|---|---|---|
| admin | admin123 | Platform admin |
| builder | builder123 | Support-center builder |
| operator | operator123 | Support-center operator |
| viewer | viewer123 | Support-center read-only |
| finance | finance123 | Finance builder |

Demo spaces: Support Center, Finance, Marketing. Space roles low→high: viewer, operator, builder, admin. Platform admin is a user flag, not a per-space role. Requests carry a login token and `X-Space-Id`.

## Console

Left nav is the control plane (resources, orchestration, governance):

1. Overview: live agent/workflow cards (refresh every 3s, collapsible; click into config/run), department calls, resources, and model/MCP usage by secret. See below
2. Agents: versions, system prompt, tools, publish, run. Studio can enable **streaming** (off by default; local agents only). Platform admin may create agents with no department (shown as “Platform”)
3. Tools: built-in registry and MCP connections. Access tokens go into the current department vault
4. Models: pick from the pool by task type and price; `provider=rules` stays offline. API keys are stored under the **model’s department**; leave blank on edit to keep, enter a new key to rebind and clean unused old secrets
5. Knowledge bases
6. Memory
7. Skills: instructions for the model; scripts run in an isolated container. Upload zips with `SKILL.md`. Detail shows storage path. See [docs/skills.md](docs/skills.md)
8. Ask: multi-turn chat; switch to “Generate workflow” to draft. Replies show version, tool count, tokens, cost, stop reason. See below
9. Workflows: draft vs published; operators may only run published graphs. Run / online test can enable **streaming** (off by default; not available for schedules). Confirm model/cost before run or schedule; schedules may set a token cap. Skills can be narrowed per node / run
10. Run history: version, tools, tokens, cost, stop reason; jump back to the workflow
11. Traces: steps and cost; quality votes; return from modal or detail page
12. Evals
13. Approvals: high-risk writes and Ask-drafted workflows. Pending drafts can be renamed/edited; only after approval are they stored and published. Jump back to Ask or workflow
14. Governance: members, secrets, ledger, audit, context policy, retention, department token budget. Only platform/department admins add members
15. Departments: only platform admin create/delete. Department admins view/edit their departments and grant models, workflows, knowledge, tools, skills

## Ask

Page: `/ask`. Left: session list (search, create). Members and department admins see the current department; platform admin sees all departments. Hover a session for “···”: rename, pin, copy share link, multi-delete, delete. Pinned sessions form their own group. The splitter is draggable (width stored locally). Pick an agent on the right. Bottom toggles “Ask” vs “Generate workflow”. On send, the question appears immediately and the input clears. Replies stream token fragments; if the model path does not emit deltas, the finished text is still chunked into short SSE pieces. Enter sends; Shift+Enter newline. The first question becomes the title (max 24 chars). Markdown rendering; tool calls expand input/output; copy or open the trace. Assistant footer shows version, tool count, tokens, cost, stop reason (same formatting as overview: tokens use “万” past 10k; cost in 元). On failure, the question stays; only the unfinished assistant bubble is removed (input is not restored).

Follow-ups include prior turns. Connected models loop up to 20 rounds. Within one dialogue, the same tool+args that already succeeded is not called again; if the model only repeats that call, the loop stops and asks for a final answer. Different args still run; failed or bad-arg calls may retry. If round 20 is still a new tool call, the run stops at the limit; “Continue” resumes with saved dialogue and tool results, still skipping successful identical args. Math uses KaTeX.

Permissions:

| Action | Who |
|---|---|
| View, search | Viewer+. Members: own sessions in current dept; dept admin: all in dept; platform admin: all depts |
| Create, retitle, switch agent, delete | Operator+ on own sessions; dept admin may edit/delete dept sessions; platform admin across depts |
| Send, continue, generate workflow | Operator+ in own sessions |

Builders, dept admins, and platform admins use the latest draft; operators use published versions. Department members only pick agents in their dept. Platform admin may pick any department’s agent; the turn is billed to that agent’s department and its model grants/quota. Agents with no department appear only for platform admin as “Platform”; the chat is attributed to the admin’s current department for quota, but any enabled model endpoint may be used. Department members cannot start sessions on those agents.

### Generate workflow from Ask

Switch to “Generate workflow”, enter a task, send → `POST /api/chats/{id}/workflow`: creates a **pending approval draft only**, not a list row. In chat you can “Edit draft” (name / node JSON) or open Approvals; after approval the graph is stored as **published**. Approvals, chats, and workflows carry the original task and ids for cross-links.

**Prompt text is largely the same across agents.** The selected agent’s full system prompt is not injected. Differences:

| Item | Behavior |
|---|---|
| Model | Prefer the agent’s bound model; else a stronger non-`rules` model already granted to the department |
| Attribution | `agent_id` = selected agent (approval / trace) |
| Candidate list | All agents in that agent’s department; role line = first line of each version’s system prompt (max 80 chars) |

Model input is exactly two messages: `system` (fixed drafting instructions + roster) + `user` (raw task). **No** tools, **no** chat history. See `conversations.propose_workflow`.

Full example (department has coordinator, planner, writer, reviewer, etc.; refund handoff task):

```json
[
  {
    "role": "system",
    "content": "根据任务描述设计一条工作流，只输出一个 JSON 对象，不要解释。\nkind 用 agents 表示按连线依次执行，用 autonomous 表示第一个节点调度后面的成员。\nnodes 里只写 agent_id，必须来自下面的编号。至少两个节点。edges 用 src、dst，节点编号按出现顺序写成 n1、n2。\n可用智能体：\n- 4 编排者：编排者已接单。未点名的子任务由编排者自己执行。句子里出现规划者、研究员、写作者、审稿人或执行者时，分发给对应角色。\n- 5 规划者：规划者已列出步骤。顺序是检索、起草、核对，再交给执行者处理可计算的部分。\n- 7 写作者：写作者已完成草稿。写作者只负责成稿，不负责检索和终审。\n- 8 审稿人：审稿人已完成核对。审稿人检查是否有依据、是否跑题，不直接改业务数据。\n（同部门其余智能体同样一行）"
  },
  {
    "role": "user",
    "content": "请为客服中心「要把一笔退款交给财务部处理」起草一条工作流。节点依次用规划者、写作者、审稿人，并在流程里计算 80 + 48。"
  }
]
```

Expected model JSON shape:

```json
{
  "name": "生成的工作流",
  "kind": "agents",
  "nodes": [{"agent_id": 5}, {"agent_id": 7}, {"agent_id": 8}],
  "edges": [{"src": "n1", "dst": "n2"}, {"src": "n2", "dst": "n3"}]
}
```

The backend parses into an approval draft. Per-node prompts used at **execution** time are configured later on the workflow, not in this draft request. More detail: [docs/ask-workflow.md](docs/ask-workflow.md).

APIs in `backend/app/api/routers/conversations.py`:

| Method | Path | Role |
|---|---|---|
| GET | `/api/chats?q=` | Search by title or user text |
| POST | `/api/chats` | Create; body includes `agent_id` |
| GET | `/api/chats/{id}` | Session and messages (assistant msgs include tokens, cost, version, approval/draft summary) |
| PATCH | `/api/chats/{id}` | Update `title`, `agent_id`, or `pinned` |
| DELETE | `/api/chats/{id}` | Delete session and messages |
| POST | `/api/chats/{id}/messages` | Send. `continue_run=true` resumes from the prior limit; `stream=true` returns SSE `delta`, `tool`, `reset`, `error`, `done` |
| POST | `/api/chats/{id}/workflow` | Draft a workflow for approval; does not insert into the workflow list |

Lists support pagination, keywords, exact fields, and time ranges. Without `page`, the API returns an array; with `page`, `{items, total, page, page_size}`.

## Agents

Agents version system prompts, models, tools, and skills. Builders, dept admins, and platform admins run the latest draft; operators run published versions. When skills are selected, their instructions append under “Available skills”; the model decides whether to call them.

### Studio run and streaming

Page: `/agents/{id}`. Configure/versions on the left; trial run on the right. Local agents (`kind=local`) can check **Streaming output**, **off by default**:

| `stream` | Behavior |
|---|---|
| `false` (default) | `POST /api/agents/{id}/run` returns the full JSON result synchronously |
| `true` | Same path as SSE: `delta` (text chunks), `tool` (tool in progress), `reset` (clear pushed text after a tool round), `error`, `done` (includes full `result`) |

With streaming on, the UI grows as tokens arrive; if the model path does not emit per-token deltas, the finished text is still chunked server-side. Remote agents do not support streaming; the API rejects `stream=true`. Internally `gateway._CHAT_EMIT` enables streamed completions; the frontend must mutate the **reactive** message object inside the array so the view updates live.

**Remote agents** (`kind=remote`) register identity and protocol locally; execution forwards to an external service while traces stay local:

| Protocol | Notes |
|---|---|
| `http_sync` | `POST` remote; sync `output` |
| `http_async` | `POST` remote with `callback_url` / `callback_token`; platform returns `waiting`, then remote calls `POST /api/agents/{id}/remote-complete` |
| `a2a` | JSON-RPC `SendMessage`; URL may be the RPC endpoint or a root that serves `/.well-known/agent-card.json` |

Remote agents do not attach local tools, skills, or MCP. **Introduction** text is for LIMEN users (role, remote tools, fit); empty by default; shown on overview and picker. Optional **health probe** (GET/POST); without it overview shows “cannot monitor live”. Callback base URL from `LIMEN_PUBLIC_BASE` (default `http://127.0.0.1:8000`).

Demo script `scripts/remote_agent_demo.py` reads `deepseek_pro` (or a deepseek-named endpoint) from the DB, starts sync/async HTTP and A2A remotes, registers introductions, and smoke-runs. Use `--serve` to keep them up. Step-by-step guide for a Python + FastAPI remote async agent: [docs/remote-async-agent.md](docs/remote-async-agent.md).

![Agent list. Each agent binds model, department, and versions.](docs/images/agents.png)

Related APIs:

| Method | Path | Role |
|---|---|---|
| POST | `/api/agents/{id}/run` | Trial run. Body: `message`, `environment`, `history`, `resume_trace_id`; `stream` defaults to `false`; for local agents `true` → SSE |
| POST | `/api/agents/{id}/remote-complete` | Async remote completion callback |

## Skills

Page: `/skills`. Instructions are for the model; script files live under `backend/data/skills/{id}/` and run only in `limen-skillbox`. The container is removed when the process exits. View/edit dialogs show “Storage path” (`storage_path` from `GET /api/skills/{id}`). New skills have no directory yet.

![Skill list. Import SKILL.md via zip; script names appear in the inventory.](docs/images/skills.png)

Which skills a department may use is set in Departments (`department_grants`, `kind=skill`). Create/import grants the current department only. Until granted elsewhere, the skill will not appear in lists, agent checkboxes, or workflows. Skills created in-dept can be edited/deleted; granted-in skills are view/use only.

Agents, workflow nodes, and the run dialog can further narrow skills for that run. Connected models call `skill_script`; rules models only see the text. Full flow, zip format, and container limits: [docs/skills.md](docs/skills.md).

## Workflows

Two graph kinds:

- `kind=agents`: pass each agent’s output along edges. Nodes with no in-edge are starts.
- `kind=autonomous`: the start agent splits subtasks by punctuation and candidate names, then runs or dispatches. Build guide: [docs/autonomous-agent.md](docs/autonomous-agent.md).

Each workflow has `release`: `draft` or `published`. Manual create starts as draft; saving a previously published definition returns it to draft until “Publish” again. Graphs created from Ask approval are published immediately. **Operators may only run published graphs**; builders/admins may still test drafts. Schedules also require published status and unused department token budget.

Before run or saving a schedule, the UI calls `GET /api/workflows/{id}/run-estimate` (agents, models, price per million tokens, remaining department tokens). Schedules may set `schedule_token_budget` (cumulative token cap; `0` = no extra cap, still under department quota); over budget disables the schedule. Cost hint ≈ 1k in + 1k out per node, same formula as billing.

### Streaming on execute

The Run dialog and editor “Online test” can enable streaming, **off by default**. Streaming is unavailable when “Schedule” is checked.

| `stream` | Behavior |
|---|---|
| `false` (default) | `POST /api/workflows/{id}/run` returns the full result synchronously |
| `true` | SSE: node progress (start/tool/complete, etc.) plus local-model `delta` / `reset` / `模型工具`; terminal events `完成` or `错误` |

Local agent nodes reuse Ask’s `_CHAT_EMIT` so text chunks enter the event queue. Graphs on rules models or remote nodes may show progress only, without token-level `delta`.

Nodes and the run dialog can change skills for that run. Outputs with `<<<FILE relative/path>>>` or path-bearing code fences can be zipped from run history. History shows version, tools, tokens, cost, stop reason, and can jump back to the workflow (with task text).

![Workflow list. Fixed steps and autonomous graphs start here.](docs/images/workflows.png)

Legacy graphs still run with `classify` / `tool` / `respond` nodes. A `tool` node’s `tool` field is the registered name and ends in `execute_builtin`.

Also: `POST /api/workflows/{id}/run` (`stream` defaults `false`), `POST /api/workflows/{id}/publish`, `GET /api/workflows/{id}/run-estimate`; approvals `PATCH /api/approvals/{id}` can edit pending draft name/`definition`.

## Department grants

Page: `/departments`. Platform admin sees all departments (create/edit/delete). Department admins see only departments they administer (view/edit; no create/delete). Builders, operators, viewers get 403 if they call the API.

![Department list. Each row is an isolation boundary and grant counts.](docs/images/departments.png)

Create/edit checks models, workflows, knowledge, tools, skills. Counts open filtered lists for that department. A department admin’s picker only shows resources they already may use. Platform-created resources stay closed until granted here. Resources created inside a department are granted to that department by default.

![Editing a department: grant models, tools, skills.](docs/images/department-grants.png)

## Governance

Page: `/governance` — tabs: members, secrets, ledger, audit. Members/secrets/ledger share the same query UX; audit is read-only.

Only platform and department admins add/edit/delete members. Department admins cannot promote to department admin (platform admin only). Platform admin appears once in the member list (not duplicated per department) and cannot be turned into a normal department member. Other roles see no add/edit/delete/bulk delete and no department picker. Platform admin can join any department; department admins only their own. New accounts get initial password `{username}123`. Existing accounts only gain membership; password unchanged.

![Governance members. Platform admin can add members and see the department column.](docs/images/governance.png)

## Overview

Page: `/overview`. Time range and department filter apply to call stats, secret stats, and agent window usage. Platform admin may view all or one department; department users see the current department. Below the filter: skill container. While the tab is visible, poll `GET /api/system` (`skillbox`) every 1s. Status shows whether the container is alive. “Script runs” lists in-flight jobs and those finished within 30s (running / just completed / just failed) with skill and file names. Field details: [docs/skills.md](docs/skills.md).

**Live agents** below the skillbox: `GET /api/observability/agents`, every 3s (paused when tab is hidden). Cards show readiness (servable / running / offline / unpublished), role summary, live version and unpublished draft flag, bound model, tool/skill/MCP counts, window calls/success/cost/tokens/avg latency/failures, last call summary, and alerts (offline, pending approval, failures, model quota, etc.). Panels and cards collapse; running or alert cards expand by default. Click → `/agents/{id}`; footer links go to traces, approvals, or last trace without navigating the whole card.

**Live workflows** next: `GET /api/observability/workflows`, every 3s. Cards: runnable / running / paused / draft / disabled, fixed vs autonomous, node count, start agent, schedule and next time, window volume/cost/tokens, last task summary, alerts (unpublished, disabled, failed, schedule quota). Click → `/workflows?open={id}`; links to run history.

Secret stats sit under model quota: model secrets (calls, tokens, cost; over time / by dept) and MCP secrets (calls; same breakdowns). Rules models have no secret and are excluded. Secret names open `/models?secret_id=` or `/tools?tab=mcp&secret_id=` filtered to that binding.

Steps with a stored secret id classify by that id. Older model steps without it use the model’s current secret. Older MCP steps only attribute when the agent version had exactly one MCP with an access token. API: `GET /api/observability/summary` → `model_secrets` / `mcp_secrets`.

## Secrets

Governance → Secrets lists the current department’s vault. Plaintext never returns to the UI or audit after save. Builders can view; department admins create/update/delete.

Each row shows how many models and MCP servers still bind that secret. Counts open filtered lists. The detail modal lists those models/MCP with department and the same jumps.

**Model API keys follow the model’s department**, not the top-bar space. Platform models with no department still land in the currently viewed department vault. Model secret names are `{model}-key`; MCP tokens still write to the **request’s department** as `{server}-mcp`.

On model edit, empty API Key keeps the binding; a new value creates another secret, rebinds, and deletes the old one if unused. Deleting a model cleans unused secrets. Secrets still referenced by a model or MCP cannot be deleted manually.

## Tokens and cost

UI **token** means model usage (input + output counts), not the login access token. Department quota is set in governance (`token_budget` / `token_used`). Prices are **CNY per million tokens**; call cost = `(tokens_in × price_in + tokens_out × price_out) / 1_000_000`. Ask, run history, overview, and pre-run confirm share `countText` (万 past 10k) and 元 for money.

## Traces

List: `/traces`. “View” opens input/output/steps. Modal footer “Back”; close stays reachable on long content. “Open page” → `/traces/{id}` with header Back to the referrer (or the list). Detail page can vote useful/not; list edit can change the rating.

## Tools and MCP

Two tabs.

**Tools** register platform functions. `tools` rows hold name, risk, review, and parameter docs—not code. Execution is `runtime.execute_builtin` by `tool.name`. Unknown names are rejected. UI-created tools belong to the current space, start pending review, and cannot be called yet.

**MCP** registers external service URLs. Tool lists come from Streamable HTTP; no repo change needed. Access tokens encrypt into the current department vault; APIs only say whether configured. Secrets page counts MCP bindings.

Built-ins today: `calculator`, `knowledge.search`, `ledger.transfer`, `python.sandbox`, `files.list`. Adding one: [docs/builtin-tools.md](docs/builtin-tools.md).

`authorize_tool` checks: registered, enabled, in the agent version’s `tool_ids`, reviewed, environment match, under per-minute rate limit.

## How agents pick tools

`classify_intent` maps keywords to `delegate`, `write`, `calculate`, `list`, `retrieve`, or `chat`. `execute_agent` then routes:

| Intent | Call |
|---|---|
| calculate | calculator |
| list | files.list |
| retrieve | knowledge.search |
| write | ledger.transfer (approval when high risk) |
| delegate | child agent, one hop only |
| chat | model chat; rules fallback if no model |

“Refund” classifies as retrieve. If the version did not enable the tool, the gate returns “tool not open on this version”.

Connected models skip keyword routing and use the tool loop in `backend/app/services/runtime/agent.py` (max 20 rounds) via API `tools`. Identical successful tool+args are not repeated; failures do not count toward that dedupe. Args compare after sorting field names.

## Tests

```bash
backend/.venv/bin/python -m pytest backend/tests/test_platform.py -q
```
