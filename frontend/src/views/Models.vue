<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, session } from "../api";
import { countText, moneyText } from "../format";
import { useDesk, when, withScope, departmentNote } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const route = useRoute();
const router = useRouter();
const desk = useDesk(() => withScope(route, "/api/models"));
const open = ref(false);
const mode = ref("create");
const form = ref(empty());
const isAdmin = ref(false);
const callsOpen = ref(false);
const calls = ref([]);
const callsTitle = ref("");
const platform = computed(() => Boolean(session().user?.platform_admin));
const columns = computed(() => [
  { key: "name", label: "名称" },
  { key: "provider", label: "提供方" },
  { key: "model", label: "模型" },
  { key: "base_url", label: "Base URL" },
  { key: "has_api_key", label: "API Key", format: (value) => (value ? "已配置" : "无") },
  { key: "price_in", label: "输入单价/百万", format: (value) => moneyText(value) },
  { key: "price_out", label: "输出单价/百万", format: (value) => moneyText(value) },
  { key: "quota_limit", label: "额度上限", format: (value) => (value ? moneyText(value) : "不限") },
  { key: "quota_used", label: "已用额度", format: (value) => moneyText(value) },
  { key: "enabled", label: "启用", format: (value) => (value ? "是" : "否") },
  ...(platform.value
    ? [
        { key: "creator", label: "创建者", format: (value) => value || "—" },
        { key: "department", label: "部门" },
      ]
    : []),
  { key: "created_at", label: "时间", format: when },
]);
function empty() {
  return { id: null, name: "", provider: "openai", model: "gpt-4o-mini", base_url: "", api_key: "", task_types: "reason", price_in: 2, price_out: 8, priority: 10, enabled: true, quota_limit: 0 };
}
function openWith(row, next) {
  form.value = row
    ? { ...empty(), ...row, task_types: (row.task_types || []).join(","), api_key: "" }
    : empty();
  mode.value = next;
  open.value = true;
}
async function save() {
  const body = { ...form.value, task_types: form.value.task_types.split(",").map((item) => item.trim()).filter(Boolean) };
  if (!body.api_key) delete body.api_key;
  if (mode.value !== "create" && !isAdmin.value) delete body.quota_limit;
  try {
    if (mode.value === "create") await api("/api/models", { method: "POST", body });
    else await api(`/api/models/${form.value.id}`, { method: "PATCH", body });
    open.value = false;
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
async function remove(row) {
  if (!window.confirm(`删除模型 ${row.name}？`)) return;
  try { await api(`/api/models/${row.id}`, { method: "DELETE" }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 个模型？`)) return;
  try { await api("/api/models/bulk-delete", { method: "POST", body: { ids: desk.selected.value } }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function openCalls(row) {
  callsTitle.value = row.name;
  try {
    calls.value = await api(`/api/models/${row.id}/calls`);
    callsOpen.value = true;
  } catch (err) {
    desk.error.value = err.message;
  }
}
function applySecretQuery() {
  if (!route.query.secret_id) return;
  desk.query.exact_field = "secret_id";
  desk.query.exact = String(route.query.secret_id);
  desk.query.page = 1;
}
onMounted(async () => {
  const space = await api("/api/spaces/current");
  isAdmin.value = space.role === "admin";
  applySecretQuery();
  await desk.refresh();
});
function clearLinkedQuery() {
  if (!route.query.secret_id) return;
  const next = { ...route.query };
  delete next.secret_id;
  router.replace({ path: route.path, query: next });
}
watch(() => route.query.secret_id, async (value) => {
  if (!value) return;
  applySecretQuery();
  await desk.refresh();
});
watch(() => route.query.usable_space, () => desk.refresh());
</script>

<template>
  <Desk kicker="04 模型池" title="模型" :sub="`API Key 只写入密钥库，列表里只显示是否已配置。额度 0 表示不限制。部门内新建的模型只给本部门；平台管理员新建的模型要在部门管理里授权后，别的部门才能使用。${departmentNote(route)}`" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :exact-fields="[{ value: 'name', label: '名称' }, { value: 'provider', label: '提供方' }, { value: 'model', label: '模型名' }, { value: 'secret_id', label: '密钥编号' }]" :error="desk.error.value" :selected="desk.selected.value" @update:selected="desk.selected.value = $event" @load="desk.refresh()" @reset="clearLinkedQuery" @create="openWith(null, 'create')" @view="openWith($event, 'view')" @edit="openWith($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #extra="{ row }">
      <button class="ghost" type="button" @click="openCalls(row)">调用历史</button>
    </template>
  </Desk>
  <Dialog :open="open" :mode="mode" :title="form.name || '模型'" @close="open = false" @submit="save">
    <label class="field"><span>名称</span><input v-model="form.name" /></label>
    <label class="field"><span>提供方</span><select v-model="form.provider"><option>openai</option><option>rules</option></select></label>
    <label class="field"><span>模型名</span><input v-model="form.model" /></label>
    <label class="field"><span>Base URL</span><input v-model="form.base_url" placeholder="https://api.openai.com/v1，可留空" /></label>
    <label class="field"><span>API Key</span><input v-model="form.api_key" type="password" autocomplete="new-password" :placeholder="mode === 'edit' ? '留空则不更换' : '没有密钥时回退规则引擎'" /></label>
    <label class="field"><span>任务，逗号分隔</span><input v-model="form.task_types" /></label>
    <div class="grid-2">
      <label class="field"><span>输入单价（每百万 token）</span><input v-model.number="form.price_in" type="number" min="0" step="0.01" /></label>
      <label class="field"><span>输出单价（每百万 token）</span><input v-model.number="form.price_out" type="number" min="0" step="0.01" /></label>
    </div>
    <label class="field"><span>优先级，数字越小越优先</span><input v-model.number="form.priority" type="number" /></label>
    <label class="field"><span>最大使用额度</span><input v-model.number="form.quota_limit" type="number" min="0" step="0.01" :disabled="mode === 'edit' && !isAdmin" /></label>
    <p class="sub">0 表示不限制。已用 {{ moneyText(form.quota_used || 0) }}，剩余 {{ form.quota_limit ? moneyText(Math.max(0, form.quota_limit - (form.quota_used || 0))) : "不限" }}。</p>
    <label class="check"><input type="checkbox" v-model="form.enabled" /> 启用</label>
    <p v-if="form.secret_name" class="sub">当前密钥 {{ form.secret_name }}</p>
  </Dialog>
  <Dialog wide :open="callsOpen" mode="view" :title="`${callsTitle} 的调用历史`" @close="callsOpen = false">
    <div class="table-scroll" tabindex="0" role="region" aria-label="模型调用历史">
      <table class="compact-table">
        <thead><tr><th>时间</th><th>调用者</th><th>部门</th><th>问题</th><th>回复</th><th>输入 token</th><th>输出 token</th><th>输入花费</th><th>输出花费</th><th>本次花费</th></tr></thead>
        <tbody>
          <tr v-if="!calls.length"><td colspan="10" class="empty-cell">还没有调用。</td></tr>
          <tr v-for="item in calls" :key="item.id">
            <td class="mono">{{ item.at }}</td>
            <td>{{ item.username }}</td>
            <td>{{ item.department }}</td>
            <td :title="item.input">{{ item.input }}</td>
            <td :title="item.output">{{ item.output }}</td>
            <td class="mono" :title="String(item.tokens_in)">{{ countText(item.tokens_in) }}</td>
            <td class="mono" :title="String(item.tokens_out)">{{ countText(item.tokens_out) }}</td>
            <td class="mono" :title="String(item.cost_in)">{{ moneyText(item.cost_in) }}</td>
            <td class="mono" :title="String(item.cost_out)">{{ moneyText(item.cost_out) }}</td>
            <td class="mono" :title="String(item.cost)">{{ moneyText(item.cost) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </Dialog>
</template>
