<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, session } from "../api";
import { useDesk, when, withScope, departmentNote } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const route = useRoute();
const router = useRouter();
const tab = ref("tools");
const desk = useDesk(() => withScope(route, "/api/tools"));
const mcp = useDesk("/api/mcp");
const open = ref(false);
const mode = ref("create");
const form = ref(empty());
const mcpOpen = ref(false);
const mcpMode = ref("create");
const mcpForm = ref(emptyMcp());
const probeNote = ref("");
const probeTools = ref([]);
const detailOpen = ref(false);
const catalog = ref({ id: null, name: "", url: "", server: "", tools: [] });
const detailError = ref("");
const platform = computed(() => Boolean(session().user?.platform_admin));
const columns = computed(() => [
  { key: "name", label: "名称" },
  { key: "access", label: "访问" },
  { key: "risk", label: "风险" },
  { key: "review_status", label: "审核" },
  { key: "owner", label: "负责人" },
  ...(platform.value
    ? [
        { key: "creator", label: "创建者", format: (value) => value || "—" },
        { key: "department", label: "部门" },
      ]
    : []),
  { key: "created_at", label: "时间", format: when },
]);
const mcpColumns = [
  { key: "name", label: "名称" },
  { key: "url", label: "地址" },
  { key: "has_token", label: "令牌", format: (value) => (value ? "已配置" : "无") },
  { key: "enabled", label: "状态", format: (value) => (value ? "启用" : "停用") },
  { key: "created_at", label: "时间", format: when },
];
function empty() {
  return { id: null, name: "", description: "", kind: "http", access: "read", risk: "low", data_flow: "internal", endpoint: "https://allow.internal/ping", owner: "", environment: "all", rate_limit_per_min: 30, enabled: true, review_note: "" };
}
function emptyMcp() {
  return { id: null, name: "", url: "", token: "", enabled: true, savedUrl: "" };
}
function openWith(row, next) {
  form.value = row ? { ...empty(), ...row } : empty();
  mode.value = next;
  open.value = true;
}
function openMcp(row, next) {
  mcpForm.value = row ? { id: row.id, name: row.name, url: row.url, token: "", enabled: row.enabled, savedUrl: row.url } : emptyMcp();
  probeNote.value = "";
  probeTools.value = [];
  mcp.error.value = "";
  mcpMode.value = next;
  mcpOpen.value = true;
  if (linkReady()) testLink(true);
}
function linkReady() {
  try {
    const url = new URL(mcpForm.value.url.trim());
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}
async function save() {
  try {
    if (mode.value === "create") await api("/api/tools", { method: "POST", body: form.value });
    else await api(`/api/tools/${form.value.id}`, { method: "PATCH", body: form.value });
    open.value = false;
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
async function saveMcp() {
  mcp.error.value = "";
  const body = { name: mcpForm.value.name, url: mcpForm.value.url, enabled: mcpForm.value.enabled, token: mcpForm.value.token };
  try {
    const saved = mcpMode.value === "create"
      ? await api("/api/mcp", { method: "POST", body })
      : await api(`/api/mcp/${mcpForm.value.id}`, { method: "PATCH", body });
    mcpOpen.value = false;
    await mcp.refresh();
    await openDetail(saved);
  } catch (err) { mcp.error.value = err.message; }
}
async function testLink(quiet = false) {
  if (!linkReady()) {
    if (!quiet) mcp.error.value = "请先填写 http 或 https 地址";
    return;
  }
  probeNote.value = "";
  if (!quiet) mcp.error.value = "";
  try {
    const keepSaved = mcpForm.value.id && !mcpForm.value.token && mcpForm.value.url === mcpForm.value.savedUrl;
    const result = keepSaved
      ? await api(`/api/mcp/${mcpForm.value.id}/test`, { method: "POST" })
      : await api("/api/mcp/probe", { method: "POST", body: { url: mcpForm.value.url, token: mcpForm.value.token } });
    probeTools.value = result.tools || [];
    const names = probeTools.value.map((item) => item.name).filter(Boolean);
    probeNote.value = names.length ? `已读取 ${names.length} 个工具的参数：${names.join("、")}` : "已连接，这个服务没有列出工具。";
    mcp.error.value = "";
  } catch (err) {
    probeTools.value = [];
    mcp.error.value = err.message;
  }
}
function schemaFields(tool) {
  const schema = tool?.input_schema || {};
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);
  return Object.entries(properties).map(([name, spec]) => ({
    name,
    type: spec?.type || "string",
    description: spec?.description || "",
    required: required.has(name),
    enum: Array.isArray(spec?.enum) ? spec.enum : [],
  }));
}
function fieldValues(tool) {
  return Object.fromEntries(schemaFields(tool).map((field) => [field.name, field.type === "boolean" ? false : ""]));
}
async function openDetail(row) {
  detailError.value = "";
  catalog.value = { id: row.id, name: row.name, url: row.url, server: "", tools: [] };
  detailOpen.value = true;
  try {
    const found = await api(`/api/mcp/${row.id}/tools`);
    catalog.value = {
      id: row.id,
      name: found.name,
      url: found.url,
      server: found.server,
      tools: (found.tools || []).map((item) => {
        const tool = { ...item, args: "{}", result: "", values: {} };
        tool.values = fieldValues(tool);
        return tool;
      }),
    };
  } catch (err) {
    detailError.value = err.message;
  }
}
function argumentsOf(tool) {
  const fields = schemaFields(tool);
  if (!fields.length) return tool.args.trim() ? JSON.parse(tool.args) : {};
  const arguments_ = {};
  for (const field of fields) {
    const raw = tool.values[field.name];
    const empty = raw === "" || raw === null || raw === undefined;
    if (empty) {
      if (field.required) throw new Error(field.description ? `请填写${field.name}（${field.description}）` : `请填写${field.name}`);
      continue;
    }
    if (field.type === "integer" || field.type === "number") {
      const number = Number(raw);
      if (Number.isNaN(number)) throw new Error(`${field.name} 需要数字`);
      arguments_[field.name] = field.type === "integer" ? Math.trunc(number) : number;
    } else if (field.type === "boolean") {
      arguments_[field.name] = Boolean(raw);
    } else if (field.type === "object" || field.type === "array") {
      arguments_[field.name] = JSON.parse(raw);
    } else {
      arguments_[field.name] = String(raw);
    }
  }
  return arguments_;
}
async function callOne(tool) {
  detailError.value = "";
  tool.result = "";
  let arguments_ = {};
  try {
    arguments_ = argumentsOf(tool);
  } catch (err) {
    tool.result = err.message;
    return;
  }
  try {
    const result = await api(`/api/mcp/${catalog.value.id}/call`, { method: "POST", body: { name: tool.name, arguments: arguments_ } });
    tool.result = result.text || "调用完成，没有文本结果。";
  } catch (err) {
    tool.result = err.message;
  }
}
async function remove(row) {
  if (!window.confirm(`删除工具 ${row.name}？`)) return;
  try { await api(`/api/tools/${row.id}`, { method: "DELETE" }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function removeMcp(row) {
  if (!window.confirm(`删除 MCP 服务 ${row.name}？`)) return;
  try { await api(`/api/mcp/${row.id}`, { method: "DELETE" }); await mcp.refresh(); } catch (err) { mcp.error.value = err.message; }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 个工具？`)) return;
  try { await api("/api/tools/bulk-delete", { method: "POST", body: { ids: desk.selected.value } }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function bulkMcp() {
  if (!window.confirm(`删除所选 ${mcp.selected.value.length} 个 MCP 服务？`)) return;
  try {
    const result = await api("/api/mcp/bulk-delete", { method: "POST", body: { ids: mcp.selected.value } });
    if (result.blocked?.length) mcp.error.value = result.blocked.map((item) => item.detail).join("；");
    await mcp.refresh();
  } catch (err) { mcp.error.value = err.message; }
}
async function review(row, decision) {
  const comment = decision === "approved" ? window.prompt("审核说明", row.data_flow === "external" ? "只读，不出域" : "通过") : "拒绝";
  if (comment === null) return;
  try { await api(`/api/tools/${row.id}/review`, { method: "POST", body: { decision, comment } }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
function applySecretQuery() {
  if (!route.query.secret_id && route.query.tab !== "mcp") return;
  if (route.query.tab === "mcp" || route.query.secret_id) tab.value = "mcp";
  if (route.query.secret_id) {
    mcp.query.exact_field = "secret_id";
    mcp.query.exact = String(route.query.secret_id);
    mcp.query.page = 1;
  }
}
onMounted(() => {
  applySecretQuery();
  desk.refresh();
  mcp.refresh();
});
function clearLinkedQuery() {
  if (!route.query.secret_id) return;
  const next = { ...route.query };
  delete next.secret_id;
  router.replace({ path: route.path, query: next });
}
watch(() => route.query.tab, (value) => {
  if (value === "mcp") tab.value = "mcp";
});
watch(() => route.query.secret_id, (value) => {
  if (!value) return;
  applySecretQuery();
  mcp.refresh();
});
watch(() => route.query.usable_space, () => desk.refresh());
</script>

<template>
  <div class="tabs">
    <button class="ghost" type="button" :aria-selected="tab === 'tools'" @click="tab = 'tools'">工具</button>
    <button class="ghost" type="button" :aria-selected="tab === 'mcp'" @click="tab = 'mcp'">MCP 服务</button>
  </div>
  <Desk v-if="tab === 'tools'" kicker="03 注册" title="工具" :sub="`未审核的工具不能被调用。全局工具只有管理员能改、能删。${departmentNote(route)}`" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :exact-fields="[{ value: 'name', label: '名称' }, { value: 'review_status', label: '审核' }, { value: 'risk', label: '风险' }, { value: 'access', label: '访问' }]" :error="desk.error.value" :selected="desk.selected.value" @update:selected="desk.selected.value = $event" @load="desk.refresh()" @create="openWith(null, 'create')" @view="openWith($event, 'view')" @edit="openWith($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #extra="{ row }">
      <button v-if="row.review_status === 'pending'" class="ghost" type="button" @click="review(row, 'approved')">通过</button>
      <button v-if="row.review_status === 'pending'" class="ghost" type="button" @click="review(row, 'rejected')">驳回</button>
    </template>
  </Desk>
  <Desk v-if="tab === 'mcp'" kicker="03 注册" title="MCP 服务" sub="添加连接后可测试是否可用。详情列出该服务上的全部工具，并能单独调用。" create-label="添加连接" :columns="mcpColumns" :rows="mcp.pageData.value.items" :total="mcp.pageData.value.total" :query="mcp.query" :exact-fields="[{ value: 'name', label: '名称' }, { value: 'secret_id', label: '密钥编号' }]" :error="mcp.error.value" :selected="mcp.selected.value" @update:selected="mcp.selected.value = $event" @load="mcp.refresh()" @reset="clearLinkedQuery" @create="openMcp(null, 'create')" @view="openMcp($event, 'view')" @edit="openMcp($event, 'edit')" @remove="removeMcp" @bulk="bulkMcp">
    <template #extra="{ row }">
      <button class="ghost" type="button" @click="openDetail(row)">详情</button>
    </template>
  </Desk>
  <Dialog :open="open" :mode="mode" :title="form.name || '工具'" @close="open = false" @submit="save">
    <label class="field"><span>名称</span><input v-model="form.name" /></label>
    <label class="field"><span>用途</span><input v-model="form.description" /></label>
    <label class="field"><span>类型</span><select v-model="form.kind"><option value="builtin">builtin · 平台内置函数</option><option value="sandbox">sandbox · 隔离表达式</option><option value="http">http · 允许域上的地址</option></select></label>
    <p class="sub">内置函数已经写在服务端：calculator、knowledge.search、ledger.transfer、python.sandbox、files.list。名称要对上这些实现，调用才会成功。要增加一种新的内置函数，需要改服务端代码并登记同名工具。MCP 页签里的函数由外部服务提供，在这里登记连接即可，不用改本仓库。</p>
    <label class="field"><span>访问</span><select v-model="form.access"><option value="read">只读</option><option value="write">写入</option></select></label>
    <label class="field"><span>风险</span><select v-model="form.risk"><option>low</option><option>medium</option><option>high</option></select></label>
    <label class="field"><span>数据流向</span><select v-model="form.data_flow"><option>internal</option><option>external</option></select></label>
    <label class="field"><span>地址</span><input v-model="form.endpoint" /></label>
    <label class="field"><span>负责人</span><input v-model="form.owner" /></label>
    <label class="field"><span>每分钟上限</span><input v-model.number="form.rate_limit_per_min" type="number" /></label>
    <p v-if="form.review_note" class="sub">审核说明：{{ form.review_note }}</p>
    <template #footer>
      <div v-if="form.detail" class="sheet">
        <p class="kicker">实现说明</p>
        <p class="sub">{{ form.detail.summary || "这条登记没有单独的实现说明。" }}</p>
        <p v-if="form.detail.example" class="sub">调用示例：{{ form.detail.example }}</p>
        <p class="kicker">参数</p>
        <p v-if="!(form.detail.parameters || []).length" class="sub">没有调用参数。</p>
        <article v-for="param in form.detail.parameters || []" :key="param.name" class="log-line">
          <strong>{{ param.name }}</strong>
          <span class="sub"> {{ param.type }} · {{ param.required ? "必填" : "可省略" }}</span>
          <p class="sub">{{ param.description }}</p>
        </article>
      </div>
    </template>
  </Dialog>
  <Dialog :open="mcpOpen" :mode="mcpMode" :title="mcpForm.name || 'MCP 服务'" @close="mcpOpen = false" @submit="saveMcp">
    <label class="field"><span>名称</span><input v-model="mcpForm.name" /></label>
    <label class="field"><span>地址</span><input v-model="mcpForm.url" placeholder="http://127.0.0.1:3000/mcp" @change="testLink(true)" /></label>
    <label class="field"><span>访问令牌</span><input v-model="mcpForm.token" type="password" placeholder="留空则沿用已保存的令牌" @change="testLink(true)" /></label>
    <label class="check"><input type="checkbox" v-model="mcpForm.enabled" /> 启用</label>
    <template #actions>
      <button class="ghost" type="button" @click="testLink">测试连接</button>
    </template>
    <template #footer>
      <p v-if="probeNote" class="sub">{{ probeNote }}</p>
      <div v-for="tool in probeTools" :key="tool.name" class="sheet">
        <strong>{{ tool.name }}</strong>
        <p class="sub">{{ tool.description || "没有说明" }}</p>
        <article v-for="field in schemaFields(tool)" :key="field.name" class="log-line">
          <strong>{{ field.name }}</strong>
          <span class="sub"> {{ field.type }} · {{ field.required ? "必填" : "可省略" }}</span>
          <p v-if="field.description" class="sub">{{ field.description }}</p>
        </article>
        <p v-if="!schemaFields(tool).length" class="sub">这个工具没有声明参数。</p>
      </div>
      <p v-if="mcp.error.value" class="sub">{{ mcp.error.value }}</p>
    </template>
  </Dialog>
  <Dialog :open="detailOpen" mode="view" :title="catalog.name || 'MCP 详情'" @close="detailOpen = false">
    <p class="sub">{{ catalog.server || "服务" }} · {{ catalog.url }}</p>
    <p v-if="detailError" class="sub">{{ detailError }}</p>
    <template #footer>
      <div v-for="tool in catalog.tools" :key="tool.name" class="sheet">
        <strong>{{ tool.name }}</strong>
        <p class="sub">{{ tool.description || "没有说明" }}</p>
        <label v-for="field in schemaFields(tool)" :key="field.name" class="field">
          <span>{{ field.name }} · {{ field.type }} · {{ field.required ? "必填" : "可省略" }}</span>
          <p v-if="field.description" class="sub">{{ field.description }}</p>
          <select v-if="field.enum.length" v-model="tool.values[field.name]">
            <option value="">不填</option>
            <option v-for="option in field.enum" :key="option" :value="String(option)">{{ option }}</option>
          </select>
          <input v-else-if="field.type === 'integer' || field.type === 'number'" v-model="tool.values[field.name]" type="number" />
          <span v-else-if="field.type === 'boolean'" class="check"><input v-model="tool.values[field.name]" type="checkbox" /> 是</span>
          <textarea v-else-if="field.type === 'object' || field.type === 'array'" v-model="tool.values[field.name]" rows="3" />
          <input v-else v-model="tool.values[field.name]" />
        </label>
        <p v-if="!schemaFields(tool).length" class="sub">这个工具没有声明参数。</p>
        <label v-if="!schemaFields(tool).length" class="field"><span>参数 JSON</span><textarea v-model="tool.args" rows="3" /></label>
        <button class="ghost" type="button" @click="callOne(tool)">调用</button>
        <p v-if="tool.result" class="sub">{{ tool.result }}</p>
      </div>
      <p v-if="!catalog.tools.length && !detailError" class="sub">没有工具。</p>
    </template>
  </Dialog>
</template>
