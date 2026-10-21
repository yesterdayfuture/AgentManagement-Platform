<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { api, session } from "../api";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";
import { applyGuides, guideText, skillGuideText } from "../toolGuide";

const router = useRouter();
const desk = useDesk("/api/agents");
const tools = ref([]);
const models = ref([]);
const servers = ref([]);
const skills = ref([]);
const open = ref(false);
const mode = ref("create");
const platform = computed(() => Boolean(session().user?.platform_admin));
const spaces = computed(() => session().spaces || []);
const form = ref(null);
const droppedModel = ref("");
const mcpCatalog = ref({});
const columns = computed(() => [
  { key: "name", label: "名称" },
  { key: "key", label: "标识" },
  {
    key: "kind",
    label: "类型",
    format: (_value, row) => (row.kind === "remote" ? `远程 · ${row.remote_protocol || "—"}` : "本地"),
  },
  { key: "model_name", label: "模型", format: (value, row) => (row.kind === "remote" ? "—" : value || "自动") },
  { key: "status", label: "状态" },
  { key: "stable_version", label: "稳定版", format: (value) => (value ? `v${value}` : "—") },
  ...(platform.value
    ? [
        { key: "creator", label: "创建者", format: (value) => value || "—" },
        { key: "department", label: "部门" },
      ]
    : []),
  { key: "created_at", label: "时间", format: when },
]);

function empty() {
  const home = spaces.value[0]?.id || "";
  return {
    id: null,
    name: "",
    key: "",
    kind: "local",
    system_prompt: "你是业务助手。金额交给计算器。",
    require_plan: true,
    tool_names: ["calculator"],
    model_id: "",
    mcp_ids: [],
    skill_ids: [],
    space_id: home,
    remote_protocol: "http_sync",
    remote_endpoint: "",
    remote_token: "",
    remote_timeout_ms: 30000,
    remote_health_url: "",
    remote_health_method: "GET",
    has_remote_token: false,
  };
}
const isRemote = computed(() => form.value?.kind === "remote");
form.value = empty();
watch(
  () => form.value?.kind,
  (kind, prev) => {
    if (mode.value !== "create" || !form.value || kind === prev) return;
    if (kind === "remote") form.value.system_prompt = "";
    else if (!(form.value.system_prompt || "").trim()) form.value.system_prompt = "你是业务助手。金额交给计算器。";
  },
);
function asList(payload) {
  return Array.isArray(payload) ? payload : payload?.items || [];
}
async function loadModels(spaceId) {
  if (platform.value && !spaceId) {
    models.value = asList(await api("/api/models"));
    return;
  }
  const id = spaceId || session().spaceId;
  models.value = asList(await api(`/api/models?usable_space=${id}`));
}
function keepGrantedModel() {
  const chosen = Number(form.value.model_id);
  if (!chosen) return;
  if (models.value.some((item) => item.id === chosen)) return;
  droppedModel.value = form.value.model_name || "已选模型";
  form.value.model_id = "";
}
async function prepare(row, nextMode) {
  const detail = row ? await api(`/api/agents/${row.id}`) : null;
  const latest = detail?.versions?.at(-1);
  droppedModel.value = "";
  form.value = detail
    ? {
        id: detail.id,
        name: detail.name,
        key: detail.key,
        kind: detail.kind || "local",
        system_prompt: latest?.system_prompt || "",
        require_plan: detail.require_plan,
        tool_names: latest?.tools || [],
        model_id: latest?.model_id || "",
        model_name: latest?.model_name || "",
        mcp_ids: latest?.mcp_ids || [],
        skill_ids: latest?.skill_ids || [],
        space_id: detail.space_id || "",
        department: detail.department || "",
        creator: detail.creator || "",
        workload_id: detail.workload_id,
        status: detail.status,
        remote_protocol: detail.remote_protocol || "http_sync",
        remote_endpoint: detail.remote_endpoint || "",
        remote_token: "",
        remote_timeout_ms: detail.remote_timeout_ms || 30000,
        remote_health_url: detail.remote_health_url || "",
        remote_health_method: detail.remote_health_method || "GET",
        has_remote_token: Boolean(detail.has_remote_token),
      }
    : empty();
  await loadModels(form.value.space_id);
  keepGrantedModel();
  mode.value = nextMode;
  if (nextMode !== "view" && form.value.kind !== "remote") await syncGuide();
  open.value = true;
}
function currentGuide() {
  return guideText({
    tools: tools.value.filter((item) => item.review_status === "approved" || form.value.tool_names.includes(item.name)),
    toolNames: form.value.tool_names,
    servers: servers.value,
    mcpIds: form.value.mcp_ids,
    catalog: mcpCatalog.value,
  });
}
function writeGuide() {
  form.value.system_prompt = applyGuides(
    form.value.system_prompt,
    currentGuide(),
    skillGuideText(skills.value, form.value.skill_ids),
  );
}
const mcpLoads = {};
async function loadMcp(id) {
  if (mcpCatalog.value[id] && !mcpCatalog.value[id].loading) return;
  if (!mcpLoads[id]) {
    mcpCatalog.value = { ...mcpCatalog.value, [id]: { tools: [], loading: true } };
    mcpLoads[id] = api(`/api/mcp/${id}/tools`)
      .then((found) => {
        mcpCatalog.value = { ...mcpCatalog.value, [id]: { tools: found.tools || [] } };
      })
      .catch((err) => {
        mcpCatalog.value = { ...mcpCatalog.value, [id]: { tools: [], error: err.message } };
      })
      .finally(() => {
        delete mcpLoads[id];
      });
  }
  await mcpLoads[id];
}
async function syncGuide() {
  await Promise.all(form.value.mcp_ids.map((id) => loadMcp(id)));
  writeGuide();
}
async function changeSpace() {
  droppedModel.value = "";
  await loadModels(form.value.space_id);
  keepGrantedModel();
}
async function toggle(name) {
  const list = form.value.tool_names;
  form.value.tool_names = list.includes(name) ? list.filter((item) => item !== name) : [...list, name];
  if (mode.value !== "view") writeGuide();
}
function toggleSkill(id) {
  const list = form.value.skill_ids;
  form.value.skill_ids = list.includes(id) ? list.filter((item) => item !== id) : [...list, id];
  if (mode.value !== "view") writeGuide();
}
async function toggleMcp(id) {
  const list = form.value.mcp_ids;
  form.value.mcp_ids = list.includes(id) ? list.filter((item) => item !== id) : [...list, id];
  if (mode.value === "view") return;
  if (form.value.mcp_ids.includes(id)) await loadMcp(id);
  writeGuide();
}
function payload() {
  if (form.value.kind === "remote") {
    return {
      name: form.value.name,
      key: form.value.key,
      kind: "remote",
      system_prompt: form.value.system_prompt || "",
      remote_protocol: form.value.remote_protocol,
      remote_endpoint: form.value.remote_endpoint,
      remote_timeout_ms: Number(form.value.remote_timeout_ms) || 30000,
      remote_health_url: form.value.remote_health_url || "",
      remote_health_method: form.value.remote_health_method || "GET",
      ...(form.value.remote_token ? { remote_token: form.value.remote_token } : mode.value === "create" ? { remote_token: "" } : {}),
      ...(platform.value && mode.value === "create"
        ? { space_id: form.value.space_id ? Number(form.value.space_id) : null, unassigned: !form.value.space_id }
        : {}),
    };
  }
  return {
    name: form.value.name,
    key: form.value.key,
    kind: "local",
    require_plan: form.value.require_plan,
    model_id: form.value.model_id || null,
    mcp_ids: form.value.mcp_ids,
    skill_ids: form.value.skill_ids,
    system_prompt: form.value.system_prompt,
    tool_names: form.value.tool_names,
    ...(platform.value && mode.value === "create"
      ? { space_id: form.value.space_id ? Number(form.value.space_id) : null, unassigned: !form.value.space_id }
      : {}),
  };
}
async function save() {
  desk.error.value = "";
  try {
    if (mode.value === "create") await api("/api/agents", { method: "POST", body: payload() });
    else await api(`/api/agents/${form.value.id}`, { method: "PATCH", body: payload() });
    open.value = false;
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function remove(row) {
  if (!window.confirm(`删除 ${row.name}？`)) return;
  try { await api(`/api/agents/${row.id}`, { method: "DELETE" }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条？`)) return;
  try {
    const result = await api("/api/agents/bulk-delete", { method: "POST", body: { ids: desk.selected.value } });
    if (result.blocked?.length) desk.error.value = result.blocked.map((item) => item.detail).join("；");
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
onMounted(async () => {
  tools.value = asList(await api("/api/tools"));
  await loadModels(session().spaceId);
  servers.value = asList(await api("/api/mcp"));
  skills.value = asList(await api("/api/skills?page=1&page_size=100"));
  await desk.refresh();
});
</script>

<template>
  <Desk kicker="02 构建" title="智能体" sub="新增、编辑、查看都在弹窗里。工作室用来配置、发布并试运行单个智能体。" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :exact-fields="[{ value: 'name', label: '名称' }, { value: 'key', label: '标识' }, { value: 'status', label: '状态' }]" :error="desk.error.value" :selected="desk.selected.value" @update:selected="desk.selected.value = $event" @load="desk.refresh()" @create="prepare(null, 'create')" @view="prepare($event, 'view')" @edit="prepare($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #extra="{ row }"><button class="ghost" type="button" @click="router.push(`/agents/${row.id}`)">工作室</button></template>
  </Desk>
  <Dialog :open="open" :mode="mode" :title="form.name || '智能体'" @close="open = false" @submit="save">
    <label v-if="platform && mode === 'create'" class="field"><span>部门</span>
      <select v-model="form.space_id" @change="changeSpace">
        <option value="">不分配部门</option>
        <option v-for="item in spaces" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
    </label>
    <p v-else-if="platform && form.department" class="sub">部门 {{ form.department }} · 创建者 {{ form.creator || "—" }}</p>
    <label class="field"><span>名称</span><input v-model="form.name" /></label>
    <label class="field"><span>标识</span><input v-model="form.key" :disabled="mode !== 'create'" /></label>
    <label class="field"><span>类型</span>
      <select v-model="form.kind" :disabled="mode !== 'create'">
        <option value="local">本地 · 本机运行</option>
        <option value="remote">远程 · 转发到外部智能体</option>
      </select>
    </label>
    <template v-if="isRemote">
      <label class="field"><span>协议</span>
        <select v-model="form.remote_protocol">
          <option value="http_sync">同步 HTTP</option>
          <option value="http_async">异步 HTTP</option>
          <option value="a2a">A2A（JSON-RPC SendMessage）</option>
        </select>
      </label>
      <label class="field"><span>远端地址</span><input v-model="form.remote_endpoint" placeholder="http://127.0.0.1:8765/run/sync" /></label>
      <label class="field"><span>访问令牌</span><input v-model="form.remote_token" :placeholder="form.has_remote_token ? '已配置，留空则不改' : '可选 Bearer 令牌'" /></label>
      <label class="field"><span>超时毫秒</span><input v-model.number="form.remote_timeout_ms" type="number" min="1000" /></label>
      <label class="field"><span>健康探测（可选）</span>
        <div class="row" style="gap: 8px; align-items: center">
          <select v-model="form.remote_health_method" style="width: 96px">
            <option value="GET">GET</option>
            <option value="POST">POST</option>
          </select>
          <input v-model="form.remote_health_url" placeholder="例如 http://127.0.0.1:18765/health，留空则总览仅显示基本信息" style="flex: 1" />
        </div>
      </label>
      <label class="field"><span>简介</span><textarea v-model="form.system_prompt" rows="5" placeholder="向本平台用户说明：这个远程智能体做什么、有哪些工具/能力、适用场景等" /></label>
      <p class="sub">简介会出现在问答选型与总览卡片上。远程智能体不挂本机工具、技能、MCP 和模型。未填健康探测时，总览标成「无法实时监测」，只展示简介与连接信息。</p>
    </template>
    <template v-else>
      <label class="field"><span>提示词</span><textarea v-model="form.system_prompt" rows="12" /></label>
      <p class="sub">勾选工具、MCP 或技能后，介绍会写进提示词末尾。工具和 MCP 在「可用工具」一段，技能在「可用技能」一段，并随勾选变化。这两段之前的文字会保留。保存会写入最新草稿。</p>
      <label class="field"><span>模型</span>
        <select v-model="form.model_id">
          <option value="">按任务自动选择</option>
          <option v-for="model in models" :key="model.id" :value="model.id">{{ model.name }} · {{ model.model }}</option>
        </select>
      </label>
      <p v-if="droppedModel" class="sub">{{ droppedModel }} 不在当前部门的可用范围内，已改为按任务自动选择。保存后生效。</p>
      <label class="check"><input type="checkbox" v-model="form.require_plan" /> 高风险先规划再审批</label>
      <p class="sub">可用工具</p>
      <div class="checks">
        <label v-for="tool in tools.filter((item) => item.review_status === 'approved')" :key="tool.id" class="check">
          <input type="checkbox" :checked="form.tool_names.includes(tool.name)" @change="toggle(tool.name)" /> {{ tool.name }}
        </label>
      </div>
      <p class="sub">可用 MCP 服务</p>
      <div class="checks">
        <label v-for="server in servers" :key="server.id" class="check">
          <input type="checkbox" :checked="form.mcp_ids.includes(server.id)" @change="toggleMcp(server.id)" /> {{ server.name }}
        </label>
        <p v-if="!servers.length" class="sub">还没有 MCP 服务。到工具页添加连接。</p>
      </div>
      <p class="sub">可用技能</p>
      <div class="checks">
        <label v-for="skill in skills" :key="skill.id" class="check">
          <input type="checkbox" :checked="form.skill_ids.includes(skill.id)" @change="toggleSkill(skill.id)" /> {{ skill.name }}
        </label>
        <p v-if="!skills.length" class="sub">还没有技能。到技能页添加说明和脚本。</p>
      </div>
    </template>
    <p v-if="mode === 'view'" class="sub">身份 {{ form.workload_id }} · {{ form.status }}</p>
  </Dialog>
</template>
