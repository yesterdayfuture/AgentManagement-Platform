<script setup>
import { computed, onMounted, ref } from "vue";
import { api, session } from "../api";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const desk = useDesk("/api/memories");
const open = ref(false);
const mode = ref("create");
const platform = computed(() => Boolean(session().user?.platform_admin));
const spaces = computed(() => session().spaces || []);
const form = ref(null);
const columns = computed(() => [
  { key: "kind", label: "类型" },
  { key: "content", label: "内容" },
  ...(platform.value
    ? [
        { key: "creator", label: "创建者", format: (value) => value || "—" },
        { key: "department", label: "部门" },
      ]
    : []),
  { key: "created_at", label: "时间", format: when },
]);
function empty() { return { id: null, kind: "long", content: "", space_id: spaces.value[0]?.id || "" }; }
form.value = empty();
function openWith(row, next) {
  form.value = row ? { id: row.id, kind: row.kind, content: row.content } : empty();
  mode.value = next;
  open.value = true;
}
async function save() {
  try {
    if (mode.value === "create") {
      const body = { kind: form.value.kind, content: form.value.content };
      if (platform.value) body.space_id = Number(form.value.space_id);
      await api("/api/memories", { method: "POST", body });
    }
    else await api(`/api/memories/${form.value.id}`, { method: "PATCH", body: form.value });
    open.value = false;
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
async function remove(row) {
  if (!window.confirm("删除这条记忆？")) return;
  try { await api(`/api/memories/${row.id}`, { method: "DELETE" }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条？`)) return;
  try { await api("/api/memories/bulk-delete", { method: "POST", body: { ids: desk.selected.value } }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function download() {
  const data = await api("/api/memories/export");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "memories.json";
  link.click();
}
onMounted(() => desk.refresh());
</script>

<template>
  <Desk kicker="10 记忆" title="记忆" sub="按用户和智能体隔离。到期的不会出现在列表里。" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :exact-fields="[{ value: 'kind', label: '类型' }, { value: 'content', label: '内容' }]" :error="desk.error.value" :selected="desk.selected.value" @update:selected="desk.selected.value = $event" @load="desk.refresh()" @create="openWith(null, 'create')" @view="openWith($event, 'view')" @edit="openWith($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #filters><button class="ghost" type="button" @click="download">导出</button></template>
  </Desk>
  <Dialog :open="open" :mode="mode" title="记忆" @close="open = false" @submit="save">
    <label v-if="platform && mode === 'create'" class="field"><span>部门</span>
      <select v-model="form.space_id"><option v-for="item in spaces" :key="item.id" :value="item.id">{{ item.name }}</option></select>
    </label>
    <label class="field"><span>类型</span><select v-model="form.kind"><option value="long">长期</option><option value="short">短期</option></select></label>
    <label class="field"><span>内容</span><textarea v-model="form.content" /></label>
  </Dialog>
</template>
