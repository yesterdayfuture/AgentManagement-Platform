<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { api, session } from "../api";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const desk = useDesk("/api/departments");
const router = useRouter();
const open = ref(false);
const mode = ref("create");
const form = ref(empty());
const catalog = ref({ models: [], workflows: [], knowledge: [], tools: [], skills: [] });
const platform = computed(() => Boolean(session().user?.platform_admin));
const columns = [
  { key: "name", label: "部门" },
  { key: "key", label: "标识" },
  { key: "member_count", label: "成员" },
  { key: "model_count", label: "模型" },
  { key: "workflow_count", label: "工作流" },
  { key: "knowledge_count", label: "知识库" },
  { key: "tool_count", label: "工具" },
  { key: "skill_count", label: "技能" },
  { key: "created_at", label: "时间", format: when },
];
const groups = [
  { key: "model_ids", catalog: "models", label: "可用模型" },
  { key: "workflow_ids", catalog: "workflows", label: "可用工作流" },
  { key: "knowledge_ids", catalog: "knowledge", label: "可用知识库" },
  { key: "tool_ids", catalog: "tools", label: "可用工具" },
  { key: "skill_ids", catalog: "skills", label: "可用技能" },
];

function empty() {
  return { id: null, name: "", model_ids: [], workflow_ids: [], knowledge_ids: [], tool_ids: [], skill_ids: [] };
}
function openWith(row, next) {
  form.value = row
    ? {
        id: row.id,
        name: row.name,
        model_ids: [...(row.model_ids || [])],
        workflow_ids: [...(row.workflow_ids || [])],
        knowledge_ids: [...(row.knowledge_ids || [])],
        tool_ids: [...(row.tool_ids || [])],
        skill_ids: [...(row.skill_ids || [])],
      }
    : empty();
  mode.value = next;
  open.value = true;
}
function toggle(key, id, event) {
  const next = new Set(form.value[key]);
  if (event.target.checked) next.add(id);
  else next.delete(id);
  form.value[key] = [...next];
}
function openCount(row, path, key) {
  localStorage.setItem("spaceId", String(row.id));
  router.push({ path, query: { [key]: String(row.id), department: row.name } });
}
async function save() {
  const body = {
    name: form.value.name,
    model_ids: form.value.model_ids,
    workflow_ids: form.value.workflow_ids,
    knowledge_ids: form.value.knowledge_ids,
    tool_ids: form.value.tool_ids,
    skill_ids: form.value.skill_ids,
  };
  try {
    if (mode.value === "create") await api("/api/departments", { method: "POST", body });
    else await api(`/api/departments/${form.value.id}`, { method: "PATCH", body });
    open.value = false;
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function remove(row) {
  if (!window.confirm(`删除部门 ${row.name}？`)) return;
  try {
    await api(`/api/departments/${row.id}`, { method: "DELETE" });
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 个部门？`)) return;
  try {
    await api("/api/departments/bulk-delete", { method: "POST", body: { ids: desk.selected.value } });
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
onMounted(async () => {
  try {
    catalog.value = await api("/api/departments/catalog");
  } catch (err) {
    desk.error.value = err.message;
  }
  await desk.refresh();
});
</script>

<template>
  <Desk
    kicker="部门"
    title="部门管理"
    sub="平台管理员可以新增部门，并勾选它可以使用的模型、工作流、知识库、工具和技能。部门管理员只能查看和编辑自己管理的部门，勾选范围限于本部门已经可以使用的资源。"
    :columns="columns"
    :rows="desk.pageData.value.items"
    :total="desk.pageData.value.total"
    :query="desk.query"
    :exact-fields="[{ value: 'name', label: '部门' }, { value: 'key', label: '标识' }]"
    :error="desk.error.value"
    :selected="desk.selected.value"
    :creatable="platform"
    :removable="platform"
    create-label="新增部门"
    @update:selected="desk.selected.value = $event"
    @load="desk.refresh()"
    @create="openWith(null, 'create')"
    @view="openWith($event, 'view')"
    @edit="openWith($event, 'edit')"
    @remove="remove"
    @bulk="bulk"
  >
    <template #cell-member_count="{ row }"><button class="linkish" type="button" @click="openCount(row, '/governance', 'space_id')">{{ row.member_count }}</button></template>
    <template #cell-model_count="{ row }"><button class="linkish" type="button" @click="openCount(row, '/models', 'usable_space')">{{ row.model_count }}</button></template>
    <template #cell-workflow_count="{ row }"><button class="linkish" type="button" @click="openCount(row, '/workflows', 'usable_space')">{{ row.workflow_count }}</button></template>
    <template #cell-knowledge_count="{ row }"><button class="linkish" type="button" @click="openCount(row, '/knowledge', 'usable_space')">{{ row.knowledge_count }}</button></template>
    <template #cell-tool_count="{ row }"><button class="linkish" type="button" @click="openCount(row, '/tools', 'usable_space')">{{ row.tool_count }}</button></template>
    <template #cell-skill_count="{ row }"><button class="linkish" type="button" @click="openCount(row, '/skills', 'usable_space')">{{ row.skill_count }}</button></template>
  </Desk>
  <Dialog :open="open" :mode="mode" wide :title="form.name || '部门'" @close="open = false" @submit="save">
    <label class="field"><span>部门名称</span><input v-model="form.name" placeholder="例如 市场部" /></label>
    <div v-for="group in groups" :key="group.key">
      <p class="sub">{{ group.label }}</p>
      <div class="checks">
        <label v-for="item in catalog[group.catalog]" :key="item.id">
          <input type="checkbox" :checked="form[group.key].includes(item.id)" @change="toggle(group.key, item.id, $event)" />
          <span>{{ item.name }}<small v-if="item.origin === 'platform'"> · 平台</small></span>
        </label>
        <p v-if="!catalog[group.catalog].length" class="sub">还没有可授权的记录。</p>
      </div>
    </div>
  </Dialog>
</template>
