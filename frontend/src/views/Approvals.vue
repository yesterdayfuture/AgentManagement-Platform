<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const desk = useDesk("/api/approvals");
const open = ref(false);
const mode = ref("view");
const form = ref({});
function flowText(row) {
  const draft = row.payload || {};
  if (row.tool_name !== "工作流" || !draft.summary) return row.comment || "";
  return `${draft.name || "工作流"} · ${draft.summary}`;
}
const columns = [
  { key: "id", label: "单号" },
  { key: "tool_name", label: "事项" },
  { key: "status", label: "状态" },
  { key: "channel", label: "渠道" },
  { key: "comment", label: "说明", format: (value, row) => flowText(row) || value },
  { key: "created_at", label: "时间", format: when },
];
function openWith(row, next) {
  form.value = { ...row, payloadText: JSON.stringify(row.payload, null, 2) };
  mode.value = next;
  open.value = true;
}
async function save() {
  try {
    await api(`/api/approvals/${form.value.id}`, { method: "PATCH", body: { comment: form.value.comment } });
    open.value = false;
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
async function remove(row) {
  if (!window.confirm(`删除审批 #${row.id}？`)) return;
  try { await api(`/api/approvals/${row.id}`, { method: "DELETE" }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条？`)) return;
  try {
    const result = await api("/api/approvals/bulk-delete", { method: "POST", body: { ids: desk.selected.value } });
    if (result.blocked?.length) desk.error.value = result.blocked.map((item) => item.detail).join("；");
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
async function decide(row, decision) {
  try {
    await api(`/api/approvals/${row.id}/decision`, { method: "POST", body: { decision, channel: "web", comment: decision === "approved" ? "门户确认" : "门户拒绝" } });
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
onMounted(() => desk.refresh());
</script>

<template>
  <Desk kicker="07 人在环里" title="审批" sub="问答里起草的工作流要在这里核对。通过后才进入工作流管理。" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :exact-fields="[{ value: 'status', label: '状态' }, { value: 'tool_name', label: '工具' }, { value: 'channel', label: '渠道' }]" :error="desk.error.value" :selected="desk.selected.value" @update:selected="desk.selected.value = $event" @load="desk.refresh()" :creatable="false" @view="openWith($event, 'view')" @edit="openWith($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #extra="{ row }">
      <button v-if="row.status === 'pending'" class="ghost" type="button" @click="decide(row, 'approved')">通过</button>
      <button v-if="row.status === 'pending'" class="ghost" type="button" @click="decide(row, 'rejected')">拒绝</button>
    </template>
  </Desk>
  <Dialog :open="open" :mode="mode" :title="`审批 #${form.id || ''}`" @close="open = false" @submit="save">
    <label class="field"><span>说明</span><input v-model="form.comment" /></label>
    <label v-if="form.payload?.task" class="field"><span>任务</span><textarea :value="form.payload.task" readonly /></label>
    <label v-if="form.payload?.summary" class="field"><span>节点</span><textarea :value="form.payload.summary" readonly /></label>
    <label class="field"><span>草案</span><textarea :value="form.payloadText" readonly /></label>
  </Dialog>
</template>
