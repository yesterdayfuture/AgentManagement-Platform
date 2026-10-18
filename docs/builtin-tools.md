# 新增内置工具

内置工具分两层。

- **登记**是数据库 `tools` 表里的一行。它决定名称、说明、参数、风险、审核和每分钟上限。查看弹窗读的是这一层，再加上 `builtins.CATALOG` 里的说明。
- **实现**是 Python 函数。智能体或工作流真正调用时，进入 `execute_builtin`，按登记名称选择函数。

只在界面点「新增」不会产生可执行函数。那条记录属于当前空间，审核状态是 `pending`，而且 `execute_builtin` 里没有同名分支，调用时返回「未知工具实现」。

要增加一种平台内置函数，需要改服务端代码，并登记同名的全局工具。下面以新增 `files.stat`（查看允许目录内某个文件的大小，不返回内容）为例。把名称和参数换成你的工具即可。现有的 `files.list` 就是按同样方式加上的。

## 现有内置工具

| 登记名称 | 实现位置 | 参数 | 智能体何时会自动调用 |
|---|---|---|---|
| calculator | `runtime.execute_builtin` | expression，可省略 | 意图 `calculate` |
| knowledge.search | `runtime.execute_builtin`，检索在 `services/retrieval.py` | query，可省略 | 意图 `retrieve` |
| ledger.transfer | `runtime.execute_builtin` | amount，必填 | 意图 `write`，经 `_write_flow` |
| python.sandbox | `runtime.execute_builtin`，执行在 `services/sandbox.py` | code，必填 | 不自动调用；工作流 tool 节点或显式参数可调用。kind 为 `sandbox` |
| files.list | `runtime.execute_builtin`，列举在 `services/files.py` 的 `list_directory` | path，可省略 | 意图 `list` |

`http` 类型没有自定义函数。`execute_builtin` 只调用 `guard_url`，地址主机必须是 `allow.internal`。

## 一次调用经过哪些函数

智能体对话：

1. `POST /api/agents/{id}/run`（`backend/app/api/routers/agents.py` 的 `run_agent`）调用 `execute_agent`。
2. `execute_agent` 调用 `classify_intent` 得到意图。
3. 对应分支调用 `_call_tool`，传入工具名称。`files.list` 的分支在 `intent == "list"`。
4. `_call_tool` 调用 `find_tool` 找登记，再调用 `authorize_tool`。
5. 通过后调用 `execute_builtin`。名称分支再调用真正的函数，例如 `list_directory`。

旧工作流的 tool 节点不经过意图分类。`run_workflow` 里 `kind == "tool"` 时，用节点上的 `node["tool"]` 和 `node["args"]` 调用同一个 `_call_tool`。

闸门 `authorize_tool`（`gateway.py`）依次拒绝这些情况：

- 没有登记或未启用：工具未注册
- 工具 id 不在当前智能体版本的 `tool_ids` 里：当前版本未开放该工具
- `review_status` 不是 `approved`：工具尚未通过发布审核
- `environment` 既不是 `all` 也不等于本次环境：工具不适用于当前环境
- 这一分钟内该智能体对该工具的次数达到 `rate_limit_per_min`：工具调用频率已熔断

连得上模型时，调用发生在工具循环里，而不是上面的关键词分流。同一段对话中，相同工具名和相同参数在成功执行后不会再次进入 `_call_tool`。参数错误和暂时失败不占用这次限制。

`find_tool` 同时查本空间和 `space_id` 为空的全局行，本空间的同名记录优先。

## 必改的位置

下面五处都要改。少一处时的表现写在每节末尾。

### 1. 写函数

新建 `backend/app/services/files_stat.py`（或把函数放进已有模块）。函数接收已经校验过的参数，返回可被 JSON 序列化的结果。失败时抛 `GateError` 或 `ValueError`。

`execute_agent` 对 `GateError` 记为拒绝或对应状态，对 `ValueError` 记为失败。不要在函数里直接改审核状态或绕过闸门。

`files.list` 的边界可以照着 `services/files.py` 的 `list_directory`：根目录默认 `backend/data/file-root`，可用环境变量 `FILES_LIST_ROOT` 改掉；解析后的路径必须仍在根目录内；不读文件内容。

### 2. `execute_builtin` 增加同名分支

文件：`backend/app/services/runtime/support.py`  
函数：`execute_builtin`

在 `raise GateError("denied", "未知工具实现")` 之前增加：

```python
if tool.name == "files.stat":
    return stat_file(args.get("path") or "")
```

顶部补上导入。函数文档字符串里的名称列表一并写上 `files.stat`。

`args` 是调用方传入的字典。智能体自动调用时经常传入 `{}`，需要的值要从 `message` 里提取，和 `calculator`、`files.list` 一样。工作流 tool 节点会把 `node["args"]` 原样传进来。

不改这里：登记和详情都在，一调用就「未知工具实现」。

`python.sandbox` 是例外：`tool.kind == "sandbox"` 时在按名称分支之前就进入 `run_sandbox`。新工具不要复用 `sandbox`，除非它就是要跑隔离表达式。

### 3. `CATALOG` 写参数说明

文件：`backend/app/services/builtins.py`  
字典：`CATALOG`

```python
"files.stat": {
    "summary": "查看允许目录内一个文件的大小，不返回内容。",
    "example": "查看文件 box/a.txt",
    "parameters": [
        {
            "name": "path",
            "type": "string",
            "required": True,
            "description": "相对允许根目录的文件路径。",
        },
    ],
},
```

`schema_of` 会把它变成 JSON Schema。`detail_of` 在 `backend/app/api/routers/tools.py` 的 `_tool_dict` 里被调用，查看弹窗底部的「实现说明」「调用示例」「参数」来自这里。名称在 `CATALOG` 里时，以代码说明为准，不看库里的旧 schema。

不改这里：函数能跑，但查看弹窗没有这条工具的参数说明。

### 4. 启动时给已有库补登记

文件：`backend/app/seed.py`  
函数：`ensure_builtin_tools`  
字典：函数内部的 `specs`

`seed()` 只在用户表为空时运行。已经在用的库不会再走 `seed()`。`core.create_app` 每次启动都会调用 `ensure_builtin_tools`。

在 `specs` 里增加一行，四元组是说明、kind、访问、风险：

```python
"files.stat": ("查看允许目录内一个文件的大小，不返回内容。", "builtin", "read", "low"),
```

循环已经是 `for name in CATALOG`。全局行不存在时，用 `specs[name]` 调用 `_tool` 插入，`space_id` 保持空，`review_status` 为 `approved`，`environment` 为 `all`。已存在但 `parameters_json` 没有 `properties` 时，用 `schema_of(name)` 补参数。已有参数不会被覆盖。

`specs` 的键必须和 `CATALOG` 一致，否则启动时 `specs[name]` 会 `KeyError`。

不改这里：新空库如果改了下一节还能看到；旧库重启后仍然没有这条工具。

### 5. 空库种子一并写入

文件：`backend/app/seed.py`  
函数：`seed`  
列表：`specs`

在元组列表中追加：

```python
("files.stat", "查看允许目录内一个文件的大小，不返回内容。", "builtin", "read", "low", "all", "", "internal"),
```

字段顺序是名称、说明、kind、access、risk、environment、endpoint、data_flow。`seed` 先把 `parameters_json` 写成 `{"type": "object"}`。同一次启动随后执行 `ensure_builtin_tools`，发现没有 `properties`，再从 `CATALOG` 补上。因此参数说明以 `CATALOG` 为准，不必在 `seed` 里再手写一份 schema。

不改这里：已有库不受影响；删掉库重新初始化时会缺这条登记。

## 让对话自动选中它

如果只通过工作流 tool 节点调用，做完上面五处即可。节点的 `tool` 填 `files.stat`，`args` 传 `{"path": "box/a.txt"}`。绑定的智能体版本仍须包含该工具 id。

如果用户一句话就要触发，再改两处。顺序不能反。

### `classify_intent`

文件：`backend/app/services/gateway.py`  
函数：`classify_intent`

当前顺序是：转交、写入、计算、列目录、检索，其余为对话。新关键词要放在会被更早规则截走的词之前。例如「退款」已经属于检索，不要指望带「退款」的句子再走到新意图。

```python
if any(word in text for word in ("文件大小", "查看文件")):
    return "stat"
```

放在 `return "chat"` 之前，并避开「文件夹」「目录」——那些词已经返回 `list`。

### `execute_agent`

文件：`backend/app/services/runtime/support.py`  
函数：`execute_agent`

在 `intent == "list"` 附近增加：

```python
elif intent == "stat":
    found = _call_tool(
        db, trace, agent, version, space, user,
        "files.stat", masked, {}, role, environment, tool_ids,
    )
    output = f"{found.get('path')}：{found.get('size')}"
```

第三个参数 `{}` 是 args。路径留空时，要在 `execute_builtin` 的 `files.stat` 分支里从 `message` 提取，否则用户必须由工作流传入 `args`。

智能体版本的 `tool_ids_json` 必须包含这条工具的 id。创建智能体时在 `tool_names` 里写 `files.stat`。已有智能体要在工作室里勾选后重新发布。

## 界面文案

查看弹窗不需要为每个工具改 Vue。`Tools.vue` 把接口返回的 `detail` 画在弹窗底部。

对话框里有一段写死的名称列表：`calculator、knowledge.search、ledger.transfer、python.sandbox、files.list`。希望提示文字也出现新名称时，改 `frontend/src/views/Tools.vue` 这句 `p.sub`。后端托管的是 `frontend/dist`，改完后在 `frontend/` 执行 `npm run build`。

## 测试

在 `backend/tests/test_platform.py` 增加测试，结构可对照 `test_files_list_reads_only_the_allowed_root`：

1. 用临时目录限制工具能碰到的范围。
2. 用 builder 登录，创建只开放 `files.stat` 的智能体。
3. `POST /api/agents/{id}/run`，断言成功结果里有期望字段。
4. 再跑一条越界输入，断言状态是 `denied`，且没有读到允许目录以外的数据。

`test_builtin_tool_detail_lists_parameters` 会请求 `GET /api/tools`。新工具若已进入 `CATALOG` 和 `ensure_builtin_tools`，可以断言 `detail.parameters` 里的参数名。

```bash
backend/.venv/bin/python -m pytest backend/tests/test_platform.py -q
```

## 改完后如何在页面上看到

重启后端。`ensure_builtin_tools` 只在进程启动时补登记。然后打开工具页，查看 `files.stat`，底部应有参数名、类型、是否必填和说明。

把该工具加入智能体版本后再运行。未加入时接口成功返回，轨迹状态是拒绝，输出是「当前版本未开放该工具」。

## 检查清单

- [ ] 实现函数，校验输入，不越过允许范围
- [ ] `runtime.execute_builtin` 按 `tool.name` 调用它
- [ ] `builtins.CATALOG` 有 summary、example、parameters
- [ ] `seed.ensure_builtin_tools` 的 `specs` 有同名四元组
- [ ] `seed.seed` 的 `specs` 列表有同名元组
- [ ] 需要对话触发时，改了 `classify_intent` 和 `execute_agent`
- [ ] 智能体版本的工具列表包含新 id
- [ ] 测试覆盖成功路径和拒绝路径
- [ ] 重启后端；改了 Vue 提示语时重新构建前端
