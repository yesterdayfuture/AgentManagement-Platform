<script setup>
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { api, session } from "../api";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const route = useRoute();
const desk = useDesk("/api/quality-reviews");
const agents = ref([]);
const workflows = ref([]);
const modal = ref(false);
const mode = ref("view");
const form = ref({ id: "", score: 5, note: "" });
const exportPurpose = ref("evaluation");
desk.query.space_id = "";
desk.query.agent_id = "";
desk.query.workflow_id = "";
const columns = [
  { key: "created_at", label: "执行时间", format: when },
  { key: "department", label: "部门" },
  { key: "agent_name", label: "智能体" },
  { key: "workflow_name", label: "工作流" },
  { key: "input", label: "输入" },
  { key: "output", label: "输出", format: value => String(value || "").replace(/^【v\d+】\s*/, "") },
  { key: "score", label: "评分", format: value => `${value}/5` },
  { key: "reviewed_at", label: "评价时间", format: when },
];
function openForm(row, next) {
  form.value = row ? { ...row, output: String(row.output || "").replace(/^【v\d+】\s*/, "") } : { id: String(route.query.trace_id || ""), score: 5, note: "" };
  mode.value = next;
  modal.value = true;
}
async function save() {
  const id = String(form.value.id || "").trim();
  if (!id) { desk.error.value = "请填写执行轨迹 ID"; return; }
  try {
    await api(mode.value === "create" ? `/api/traces/${encodeURIComponent(id)}/quality` : `/api/quality-reviews/${encodeURIComponent(id)}`, {
      method: mode.value === "create" ? "POST" : "PATCH",
      body: { score: Number(form.value.score), note: form.value.note || "" },
    });
    modal.value = false;
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
async function remove(row) {
  if (!window.confirm("删除这条评价？原始执行记录会保留。")) return;
  try { await api(`/api/quality-reviews/${encodeURIComponent(row.id)}`, { method: "DELETE" }); await desk.refresh(); }
  catch (err) { desk.error.value = err.message; }
}
async function bulk() {
  if (!window.confirm(`删除已选的 ${desk.selected.value.length} 条评价？`)) return;
  try {
    for (const id of desk.selected.value) await api(`/api/quality-reviews/${encodeURIComponent(id)}`, { method: "DELETE" });
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
async function exportFile() {
  if (!desk.selected.value.length) { desk.error.value = "请先勾选要导出的评价记录"; return; }
  const params = new URLSearchParams({ purpose: exportPurpose.value });
  desk.selected.value.forEach(id => params.append("ids", String(id)));
  const current = session();
  const response = await fetch(`/api/quality-reviews/export?${params}`, { headers: { Authorization: `Bearer ${current.token}`, "X-Space-Id": current.spaceId } });
  if (!response.ok) { desk.error.value = (await response.json().catch(() => ({}))).detail || "导出失败"; return; }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href = url; link.download = exportPurpose.value === "finetune" ? "quality-finetune.jsonl" : "quality-evaluation.json"; link.click(); URL.revokeObjectURL(url);
  desk.error.value = "";
}
onMounted(async () => {
  try {
    const [agentRows, workflowRows] = await Promise.all([api("/api/agents"), api("/api/workflows")]);
    agents.value = Array.isArray(agentRows) ? agentRows : agentRows.items || [];
    workflows.value = Array.isArray(workflowRows) ? workflowRows : workflowRows.items || [];
  } catch (err) { desk.error.value = err.message; }
  await desk.refresh();
  if (route.query.trace_id) openForm(null, "create");
});
</script>

<template>
  <Desk kicker="质量评价" title="执行质量评价" sub="对智能体和工作流的单次执行结果评分；评价自动关联输入、输出和执行对象。" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :error="desk.error.value" :selected="desk.selected.value" :exact-fields="[{ value: 'score', label: '评分' }, { value: 'status', label: '执行状态' }]" :actions-width="210" @update:selected="desk.selected.value = $event" @load="desk.refresh()" @create="openForm(null, 'create')" @view="openForm($event, 'view')" @edit="openForm($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #filters>
      <label>部门<SearchSelect v-model="desk.query.space_id" @change="desk.query.page = 1; desk.refresh()"><option value="">全部</option><option v-for="item in session().spaces" :key="item.id" :value="item.id">{{ item.name }}</option></SearchSelect></label>
      <label>智能体<SearchSelect v-model="desk.query.agent_id" @change="desk.query.page = 1; desk.refresh()"><option value="">全部</option><option v-for="item in agents" :key="item.id" :value="item.id">{{ item.name }}</option></SearchSelect></label>
      <label>工作流<SearchSelect v-model="desk.query.workflow_id" @change="desk.query.page = 1; desk.refresh()"><option value="">全部</option><option v-for="item in workflows" :key="item.id" :value="item.id">{{ item.name }}</option></SearchSelect></label>
      <label>导出用途<SearchSelect v-model="exportPurpose"><option value="evaluation">智能体评估 · JSON</option><option value="finetune">模型微调 · JSONL</option></SearchSelect></label>
    </template>
    <template #filter-actions><button class="ghost" type="button" @click="exportFile">导出已选{{ desk.selected.value.length ? `（${desk.selected.value.length}）` : "" }}</button></template>
  </Desk>
  <Dialog wide :open="modal" :mode="mode" title="执行质量评价" @close="modal = false" @submit="save">
    <label class="field"><span>执行轨迹 ID</span><input v-model="form.id" :readonly="mode !== 'create'" /></label>
    <template v-if="mode !== 'create'"><p class="sub">{{ form.department }} · 智能体 {{ form.agent_name || '—' }}{{ form.agent_version ? ` v${form.agent_version}${form.agent_version_status === 'draft' ? '（草稿）' : ''}` : '' }} · 工作流 {{ form.workflow_name || '—' }}{{ form.workflow_version ? ` v${form.workflow_version}` : '' }} · {{ form.created_at }}</p><label class="field"><span>输入</span><textarea :value="form.input" readonly rows="4" /></label><label class="field"><span>输出</span><textarea :value="form.output" readonly rows="6" /></label></template>
    <label class="field"><span>质量评分（1–5）</span><input v-model.number="form.score" type="number" min="1" max="5" :readonly="mode === 'view'" /></label>
    <label class="field"><span>评价说明</span><textarea v-model="form.note" rows="4" :readonly="mode === 'view'" /></label>
    <p v-if="mode !== 'create'" class="sub">评价人：{{ form.reviewer || '—' }} · 评价时间：{{ form.reviewed_at || '—' }}</p>
  </Dialog>
</template>
