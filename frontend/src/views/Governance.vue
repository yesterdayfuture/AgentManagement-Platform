<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { RouterLink } from "vue-router";
import { api, session } from "../api";
import { countText, moneyText } from "../format";
import { useDesk, when, withScope, departmentNote } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const route = useRoute();
const tab = ref("members");
const members = useDesk(() => withScope(route, "/api/spaces/current/members", "space_id"));
const secrets = useDesk("/api/secrets");
const audit = useDesk("/api/audit");
const ledger = useDesk("/api/ledger");
const space = ref(null);
const open = ref(false);
const mode = ref("view");
const kind = ref("members");
const form = ref({});
const memberNote = ref("");
const initialPassword = computed(() => `${(form.value.username || "").trim()}123`);
const desks = { members, secrets, audit, ledger };
const roleName = { platform: "平台管理员", admin: "部门管理员", builder: "构建者", operator: "操作者", viewer: "只读" };
const platform = computed(() => Boolean(session().user?.platform_admin));
const canManageMembers = computed(() => platform.value || space.value?.role === "admin");
const spaces = computed(() => {
  const all = session().spaces || [];
  return platform.value ? all : all.filter((item) => item.role === "admin");
});
const memberColumns = computed(() => [
  { key: "display_name", label: "姓名" },
  { key: "username", label: "账号" },
  ...(platform.value ? [{ key: "department", label: "部门" }] : []),
  { key: "role", label: "角色", format: (value) => roleName[value] || value },
  { key: "created_at", label: "时间", format: when },
]);
const secretColumns = [
  { key: "name", label: "名称" },
  { key: "model_count", label: "模型" },
  { key: "mcp_count", label: "MCP 服务" },
  { key: "created_at", label: "时间", format: when },
];
const auditColumns = [{ key: "action", label: "动作" }, { key: "actor", label: "人" }, { key: "target", label: "对象" }, { key: "detail", label: "细节" }, { key: "created_at", label: "时间", format: when }];
const ledgerColumns = [{ key: "name", label: "账户" }, { key: "balance", label: "余额", format: (value) => moneyText(value) }, { key: "created_at", label: "时间", format: when }];

function current() { return desks[tab.value]; }
async function loadSpace() {
  space.value = await api("/api/spaces/current");
}
function openMember(row, next) {
  kind.value = "members";
  form.value = row
    ? { username: row.username, role: row.role, space_id: row.space_id || session().spaceId }
    : { username: "", role: "operator", space_id: session().spaceId || spaces.value[0]?.id || "" };
  mode.value = next;
  open.value = true;
}
const secretAlphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";

function randomSecret() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => secretAlphabet[byte % secretAlphabet.length]).join("");
}

function openSecret(row, next) {
  kind.value = "secrets";
  form.value = { ...(row || {}), id: row?.id || null, name: row?.name || "", value: next === "create" ? randomSecret() : "" };
  mode.value = next;
  open.value = true;
}
function openLedger(row, next) {
  kind.value = "ledger";
  form.value = row ? { ...row } : { name: "", balance: 0 };
  mode.value = next;
  open.value = true;
}
function openPolicy(next) {
  kind.value = "policy";
  form.value = { deny: (space.value?.policy.deny || []).join("、"), mask: (space.value?.policy.mask || []).join("、"), isolation: space.value?.policy.isolation || "user,agent", retention_days: space.value?.policy.retention_days || 30, token_budget: space.value?.token_budget || 1000000 };
  mode.value = next;
  open.value = true;
}
function openAudit(row) {
  kind.value = "audit";
  form.value = { ...row };
  mode.value = "view";
  open.value = true;
}
async function save() {
  const desk = current();
  try {
    if (kind.value === "members") {
      const saved = await api("/api/spaces/current/members", { method: "POST", body: { username: form.value.username, role: form.value.role, space_id: Number(form.value.space_id) } });
      memberNote.value = saved.initial_password
        ? `已创建 ${saved.username}，初始密码 ${saved.initial_password}。登录后可在右上角修改密码。`
        : `${saved.username} 已在本部门，密码未改。`;
    }
    if (kind.value === "secrets") {
      if (mode.value === "create") await api("/api/secrets", { method: "POST", body: form.value });
      else await api(`/api/secrets/${form.value.id}`, { method: "PATCH", body: form.value });
    }
    if (kind.value === "ledger") {
      if (mode.value === "create") await api("/api/ledger", { method: "POST", body: form.value });
      else await api(`/api/ledger/${form.value.id}`, { method: "PATCH", body: form.value });
    }
    if (kind.value === "policy") {
      await api("/api/spaces/current/policy", { method: "PUT", body: { deny: form.value.deny.split(/[、,，]/).map((item) => item.trim()).filter(Boolean), mask: form.value.mask.split(/[、,，]/).map((item) => item.trim()).filter(Boolean), isolation: form.value.isolation, retention_days: Number(form.value.retention_days) } });
      await api("/api/spaces/current/quota", { method: "PUT", body: { token_budget: Number(form.value.token_budget) } });
      await loadSpace();
    }
    open.value = false;
    if (desk) await desk.refresh();
  } catch (err) {
    if (desk) desk.error.value = err.message;
  }
}
async function remove(row) {
  const paths = { members: `/api/spaces/current/members/${row.id}`, secrets: `/api/secrets/${row.id}`, audit: `/api/audit/${row.id}`, ledger: `/api/ledger/${row.id}` };
  if (!window.confirm("删除这条记录？")) return;
  try { await api(paths[tab.value], { method: "DELETE" }); await current().refresh(); } catch (err) { current().error.value = err.message; }
}
async function bulk() {
  const paths = { members: "/api/spaces/current/members/bulk-delete", secrets: "/api/secrets/bulk-delete", audit: "/api/audit/bulk-delete", ledger: "/api/ledger/bulk-delete" };
  const desk = current();
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条？`)) return;
  try { await api(paths[tab.value], { method: "POST", body: { ids: desk.selected.value } }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
onMounted(async () => {
  await loadSpace();
  await Promise.all([members.refresh(), secrets.refresh().catch((err) => { secrets.error.value = err.message; }), audit.refresh(), ledger.refresh()]);
});
watch(() => route.query.space_id, () => members.refresh());
</script>

<template>
  <p class="kicker">11 治理</p>
  <div class="top">
    <div>
      <h1>边界</h1>
      <p class="sub">成员、密钥、账本和审计都按同一套查询。审计只可查看和删除，不能改写。</p>
    </div>
    <button class="ghost" type="button" @click="openPolicy('edit')">编辑策略</button>
  </div>
  <div class="tabs">
    <button class="ghost" type="button" :aria-selected="tab === 'members'" @click="tab = 'members'">成员</button>
    <button class="ghost" type="button" :aria-selected="tab === 'secrets'" @click="tab = 'secrets'">密钥</button>
    <button class="ghost" type="button" :aria-selected="tab === 'ledger'" @click="tab = 'ledger'">账本</button>
    <button class="ghost" type="button" :aria-selected="tab === 'audit'" @click="tab = 'audit'">审计</button>
  </div>
  <p v-if="tab === 'members' && memberNote" class="sub">{{ memberNote }}</p>
  <Desk v-if="tab === 'members'" kicker="" title="" :sub="`新账号的初始密码是账号后面加 123。已有账号只加入本部门，不改密码。平台管理员单独显示一行。部门管理员不能把成员设为部门管理员。${departmentNote(route)}`" :columns="memberColumns" :rows="members.pageData.value.items" :total="members.pageData.value.total" :query="members.query" :exact-fields="[{ value: 'username', label: '账号' }, { value: 'role', label: '角色' }]" :error="members.error.value" :selected="members.selected.value" :creatable="canManageMembers" :editable="canManageMembers" :removable="canManageMembers" :row-mutable="(row) => row.role !== 'platform' && (platform || row.role !== 'admin')" @update:selected="members.selected.value = $event" @load="members.refresh()" @create="openMember(null, 'create')" @view="openMember($event, 'view')" @edit="openMember($event, 'edit')" @remove="remove" @bulk="bulk" />
  <Desk v-if="tab === 'secrets'" kicker="" title="" sub="明文不会回到页面。数字是当前仍绑定这条密钥的模型和 MCP 服务，点击后只显示这些记录。" :columns="secretColumns" :rows="secrets.pageData.value.items" :total="secrets.pageData.value.total" :query="secrets.query" :exact-fields="[{ value: 'name', label: '名称' }]" :error="secrets.error.value" :selected="secrets.selected.value" @update:selected="secrets.selected.value = $event" @load="secrets.refresh()" @create="openSecret(null, 'create')" @view="openSecret($event, 'view')" @edit="openSecret($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #cell-model_count="{ row }">
      <RouterLink :to="`/models?secret_id=${row.id}`">{{ row.model_count || 0 }} 个模型</RouterLink>
    </template>
    <template #cell-mcp_count="{ row }">
      <RouterLink :to="`/tools?tab=mcp&secret_id=${row.id}`">{{ row.mcp_count || 0 }} 个 MCP</RouterLink>
    </template>
  </Desk>
  <Desk v-if="tab === 'ledger'" kicker="" title="" sub="改余额会记入审计。至少保留一个账户。" :columns="ledgerColumns" :rows="ledger.pageData.value.items" :total="ledger.pageData.value.total" :query="ledger.query" :exact-fields="[{ value: 'name', label: '账户' }]" :error="ledger.error.value" :selected="ledger.selected.value" @update:selected="ledger.selected.value = $event" @load="ledger.refresh()" @create="openLedger(null, 'create')" @view="openLedger($event, 'view')" @edit="openLedger($event, 'edit')" @remove="remove" @bulk="bulk" />
  <Desk v-if="tab === 'audit'" kicker="" title="" sub="" :columns="auditColumns" :rows="audit.pageData.value.items" :total="audit.pageData.value.total" :query="audit.query" :exact-fields="[{ value: 'action', label: '动作' }, { value: 'actor', label: '人' }]" :error="audit.error.value" :selected="audit.selected.value" :creatable="false" :editable="false" @update:selected="audit.selected.value = $event" @load="audit.refresh()" @view="openAudit" @remove="remove" @bulk="bulk" />
  <Dialog :open="open" :mode="mode" :title="kind === 'policy' ? '上下文策略' : '记录'" @close="open = false" @submit="save">
    <template v-if="kind === 'members'">
      <template v-if="form.role === 'platform'">
        <p class="sub">{{ form.display_name || form.username }}（{{ form.username }}）是平台管理员，管理全部部门，不加入某个部门，也不能在这里修改或删除。</p>
      </template>
      <template v-else>
        <label v-if="platform" class="field"><span>部门</span>
          <select v-model="form.space_id" :disabled="mode !== 'create'">
            <option v-for="item in spaces" :key="item.id" :value="String(item.id)">{{ item.name }}</option>
          </select>
        </label>
        <label class="field"><span>账号</span><input v-model="form.username" :disabled="mode !== 'create'" /></label>
        <label class="field"><span>角色</span><select v-model="form.role"><option value="viewer">只读</option><option value="operator">操作者</option><option value="builder">构建者</option><option v-if="platform || mode === 'view'" value="admin">部门管理员</option></select></label>
        <p v-if="mode === 'create'" class="sub">{{ (form.username || "").trim() ? `新账号初始密码为 ${initialPassword}。` : "填写账号后，初始密码是账号后面加 123。" }}已有账号不会重置密码。</p>
      </template>
    </template>
    <template v-else-if="kind === 'secrets'">
      <label class="field"><span>名称</span><input v-model="form.name" /></label>
      <label class="field"><span>值</span><input v-model="form.value" :type="mode === 'create' ? 'text' : 'password'" spellcheck="false" autocomplete="off" :placeholder="mode === 'edit' ? '留空则不更换' : ''" /></label>
      <div v-if="mode === 'create'" class="row">
        <button class="ghost" type="button" @click="form.value = randomSecret()">重新生成</button>
      </div>
      <p v-if="mode === 'create'" class="sub">32 位大小写字母和数字。保存后明文不再显示。</p>
    </template>
    <template v-else-if="kind === 'ledger'">
      <label class="field"><span>账户</span><input v-model="form.name" /></label>
      <label class="field"><span>余额</span><input v-model.number="form.balance" type="number" step="0.01" /></label>
    </template>
    <template v-else-if="kind === 'policy'">
      <label class="field"><span>禁止进入模型，顿号分隔</span><input v-model="form.deny" /></label>
      <label class="field"><span>脱敏字段，顿号分隔</span><input v-model="form.mask" /></label>
      <label class="field"><span>隔离维度</span><input v-model="form.isolation" /></label>
      <label class="field"><span>记忆保留天数</span><input v-model.number="form.retention_days" type="number" /></label>
      <label class="field"><span>令牌额度</span><input v-model.number="form.token_budget" type="number" /></label>
    </template>
    <template v-else>
      <p>{{ form.action }} · {{ form.actor }}</p>
      <p>{{ form.target }} {{ form.detail }}</p>
      <p class="sub">{{ form.created_at }}</p>
    </template>
    <template #footer v-if="kind === 'secrets' && form.id">
      <div class="secret-usage">
        <p class="sub">当前有 {{ form.model_count || 0 }} 个模型、{{ form.mcp_count || 0 }} 个 MCP 服务使用这条密钥。</p>
        <p v-if="form.models?.length"><RouterLink :to="`/models?secret_id=${form.id}`">查看使用它的模型</RouterLink>：{{ form.models.map((item) => `${item.name}（${item.department}）`).join("、") }}</p>
        <p v-if="form.mcp_servers?.length"><RouterLink :to="`/tools?tab=mcp&secret_id=${form.id}`">查看使用它的 MCP 服务</RouterLink>：{{ form.mcp_servers.map((item) => `${item.name}（${item.department}）`).join("、") }}</p>
      </div>
    </template>
  </Dialog>
</template>
