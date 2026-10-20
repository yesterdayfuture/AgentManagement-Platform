<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const router = useRouter();
const desk = useDesk("/api/approvals");
const open = ref(false);
const mode = ref("view");
const form = ref({});
const saving = ref(false);

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
const isWorkflowDraft = computed(() => form.value.tool_name === "工作流");
const canEditDraft = computed(() => isWorkflowDraft.value && form.value.status === "pending" && mode.value === "edit");

function openWith(row, next) {
  const payload = row.payload || {};
  const definition = payload.definition || {};
  form.value = {
    ...row,
    draftName: payload.name || "",
    draftTask: payload.task || row.task || "",
    draftSummary: payload.summary || "",
    draftKind: definition.kind || payload.kind || "agents",
    draftNodes: JSON.stringify(definition.nodes || [], null, 2),
    draftEdges: JSON.stringify(definition.edges || [], null, 2),
    payloadText: JSON.stringify(payload, null, 2),
  };
  mode.value = next;
  open.value = true;
}
async function save() {
  saving.value = true;
  desk.error.value = "";
  try {
    const body = { comment: form.value.comment };
    if (canEditDraft.value) {
      let nodes;
      let edges;
      try {
        nodes = JSON.parse(form.value.draftNodes || "[]");
        edges = JSON.parse(form.value.draftEdges || "[]");
      } catch {
        throw new Error("节点或连线不是合法 JSON");
      }
      if (!Array.isArray(nodes) || !nodes.length) throw new Error("至少保留一个节点");
      body.name = form.value.draftName;
      body.definition = {
        kind: form.value.draftKind === "autonomous" ? "autonomous" : "agents",
        entry: nodes[0]?.id || "n1",
        nodes,
        edges: Array.isArray(edges) ? edges : [],
      };
    }
    const saved = await api(`/api/approvals/${form.value.id}`, { method: "PATCH", body });
    open.value = false;
    await desk.refresh();
    if (saved.payload?.summary) desk.error.value = "";
  } catch (err) {
    desk.error.value = err.message;
  } finally {
    saving.value = false;
  }
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
    const result = await api(`/api/approvals/${row.id}/decision`, { method: "POST", body: { decision, channel: "web", comment: decision === "approved" ? "门户确认" : "门户拒绝" } });
    await desk.refresh();
    if (decision === "approved" && result.workflow_id) {
      router.push({ path: "/workflows", query: { open: String(result.workflow_id), task: row.payload?.task || row.task || "" } });
    }
  } catch (err) { desk.error.value = err.message; }
}
function goChat(row) {
  const chat = row.chat_session_id || row.payload?.chat_session_id;
  if (chat) router.push({ path: "/ask", query: { chat: String(chat) } });
}
function goWorkflow(row) {
  const id = row.workflow_id || row.payload?.workflow_id;
  if (id) router.push({ path: "/workflows", query: { open: String(id), task: row.task || row.payload?.task || "" } });
}
onMounted(() => desk.refresh());
</script>

<template>
  <Desk kicker="07 人在环里" title="审批" sub="问答里起草的工作流要在这里核对，可改名和改节点。通过后才进入工作流管理并发布。" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :exact-fields="[{ value: 'status', label: '状态' }, { value: 'tool_name', label: '工具' }, { value: 'channel', label: '渠道' }]" :error="desk.error.value" :selected="desk.selected.value" @update:selected="desk.selected.value = $event" @load="desk.refresh()" :creatable="false" @view="openWith($event, 'view')" @edit="openWith($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #extra="{ row }">
      <button v-if="row.chat_session_id || row.payload?.chat_session_id" class="ghost" type="button" @click="goChat(row)">原问答</button>
      <button v-if="row.workflow_id || row.payload?.workflow_id" class="ghost" type="button" @click="goWorkflow(row)">工作流</button>
      <button v-if="row.status === 'pending'" class="ghost" type="button" @click="decide(row, 'approved')">通过</button>
      <button v-if="row.status === 'pending'" class="ghost" type="button" @click="decide(row, 'rejected')">拒绝</button>
    </template>
  </Desk>
  <Dialog :open="open" :mode="mode" :title="`审批 #${form.id || ''}`" @close="open = false" @submit="save">
    <label class="field"><span>说明</span><input v-model="form.comment" /></label>
    <label v-if="form.draftTask" class="field"><span>原任务</span><textarea v-model="form.draftTask" readonly /></label>
    <template v-if="isWorkflowDraft">
      <label class="field"><span>草案名称</span><input v-model="form.draftName" :readonly="!canEditDraft" /></label>
      <label class="field"><span>执行方式</span>
        <select v-model="form.draftKind" :disabled="!canEditDraft">
          <option value="agents">固定步骤</option>
          <option value="autonomous">自主调度</option>
        </select>
      </label>
      <label class="field"><span>节点 JSON</span><textarea v-model="form.draftNodes" rows="8" :readonly="!canEditDraft" /></label>
      <label class="field"><span>连线 JSON</span><textarea v-model="form.draftEdges" rows="4" :readonly="!canEditDraft" /></label>
      <p v-if="form.draftSummary" class="sub">摘要：{{ form.draftSummary }}</p>
    </template>
    <label v-else class="field"><span>草案</span><textarea :value="form.payloadText" readonly /></label>
    <div class="row" style="margin-top: 8px; gap: 8px;">
      <button v-if="form.chat_session_id || form.payload?.chat_session_id" class="ghost" type="button" @click="goChat(form)">打开原问答</button>
      <button v-if="form.workflow_id || form.payload?.workflow_id" class="ghost" type="button" @click="goWorkflow(form)">打开工作流</button>
    </div>
  </Dialog>
</template>
