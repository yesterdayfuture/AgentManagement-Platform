<script setup>
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { renderMarkdown } from "../markdown";
import { durationText, prettyJson } from "../format";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const route = useRoute();
const router = useRouter();
const desk = useDesk("/api/workflow-runs");
const open = ref(false);
const detail = ref(null);
const columns = [
  { key: "workflow_name", label: "工作流" },
  { key: "message", label: "问题", format: (value) => brief(value) },
  { key: "status", label: "状态", format: (value) => statusText(value) },
  { key: "environment", label: "环境" },
  { key: "duration_ms", label: "总耗时", format: (value) => duration(value) },
  { key: "actor", label: "执行人" },
  { key: "started_at", label: "开始", format: when },
];

function statusText(value) {
  return (
    {
      running: "运行中",
      paused: "已暂停",
      stopped: "已停止",
      completed: "已完成",
      failed: "失败",
      blocked: "已熔断",
      denied: "已拒绝",
      suspended: "待审批",
    }[value] || value || "—"
  );
}
function markdown(value) {
  return renderMarkdown(value);
}
async function downloadProject() {
  const headers = {};
  const token = localStorage.getItem("token");
  const spaceId = localStorage.getItem("spaceId");
  if (token) headers.Authorization = `Bearer ${token}`;
  if (spaceId) headers["X-Space-Id"] = spaceId;
  const response = await fetch(`/api/workflow-runs/${detail.value.id}/archive`, { headers });
  if (!response.ok) {
    desk.error.value = "这次执行没有可下载的项目文件";
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${detail.value?.workflow_name || "项目"}-${detail.value.id}.zip`;
  link.click();
  URL.revokeObjectURL(url);
}
function download() {
  const name = `${detail.value?.workflow_name || "执行"}-${detail.value?.id || ""}.md`;
  const blob = new Blob([detail.value?.output || ""], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}
function brief(value) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return "—";
  return text.length > 42 ? `${text.slice(0, 42)}…` : text;
}
function duration(value) {
  if (value === null || value === undefined || value === "") return "—";
  const ms = Number(value);
  if (Number.isNaN(ms)) return "—";
  return durationText(ms);
}
async function show(row) {
  try {
    detail.value = await api(`/api/workflow-runs/${row.id}`);
    open.value = true;
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function remove(row) {
  if (!window.confirm(`删除执行 #${row.id}？`)) return;
  try {
    await api(`/api/workflow-runs/${row.id}`, { method: "DELETE" });
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条执行？`)) return;
  try {
    const result = await api("/api/workflow-runs/bulk-delete", { method: "POST", body: { ids: desk.selected.value } });
    if (result.blocked?.length) desk.error.value = result.blocked.map((item) => item.detail).join("；");
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
function clearLinkedQuery() {
  if (!route.query.workflow_id) return;
  const next = { ...route.query };
  delete next.workflow_id;
  router.replace({ path: route.path, query: next });
}
onMounted(() => {
  if (route.query.workflow_id) {
    desk.query.exact_field = "workflow_id";
    desk.query.exact = String(route.query.workflow_id);
  }
  desk.refresh();
});
</script>

<template>
  <Desk
    kicker="07 执行"
    title="执行结果"
    sub="每一行都能看到这次输入的问题。查看时展示每个智能体的提示词、每一步的输入输出和耗时，以及最终输出和总耗时。"
    empty-text="还没有执行结果。"
    :creatable="false"
    :editable="false"
    :columns="columns"
    :rows="desk.pageData.value.items"
    :total="desk.pageData.value.total"
    :query="desk.query"
    :exact-fields="[{ value: 'status', label: '状态' }, { value: 'environment', label: '环境' }, { value: 'workflow_id', label: '工作流编号' }]"
    :error="desk.error.value"
    :selected="desk.selected.value"
    @update:selected="desk.selected.value = $event"
    @load="desk.refresh()"
    @reset="clearLinkedQuery"
    @view="show"
    @remove="remove"
    @bulk="bulk"
  />
  <Dialog wide :open="open" mode="view" :title="detail?.workflow_name || '执行详情'" @close="open = false">
    <p class="sub" v-if="detail">
      {{ statusText(detail.status) }} · {{ detail.environment }} · 总耗时 {{ duration(detail.duration_ms) }} · {{ detail.actor || "—" }}
    </p>
    <p class="sub" v-if="detail">开始 {{ detail.started_at || "—" }} · 结束 {{ detail.finished_at || "仍在进行" }}</p>
    <label class="field"><span>问题</span><textarea :value="detail?.message || ''" readonly rows="4" /></label>
    <p class="kicker">最终输出</p>
    <div class="md" v-html="markdown(detail?.output || '')"></div>
    <p class="kicker">步骤</p>
    <article v-for="(line, index) in detail?.logs || []" :key="index" class="log-line">
      <strong>{{ line.agent || line.node || "步骤" }} · {{ line.event }}{{ line.tool ? ` · ${line.tool}` : "" }}</strong>
      <span class="sub"> {{ statusText(line.status) }} · 耗时 {{ duration(line.ms) }}</span>
      <label v-if="line.prompt" class="field"><span>提示词</span><textarea :value="line.prompt" readonly rows="4" /></label>
      <label class="field"><span>{{ line.event === "工具" ? "入参" : "输入" }}</span><textarea :value="line.event === '工具' ? prettyJson(line.input) : (line.input || '—')" readonly rows="3" /></label>
      <label class="field"><span>输出</span><textarea :value="line.output || '—'" readonly rows="4" /></label>
    </article>
    <p v-if="detail && !detail.logs?.length" class="sub">这次执行没有留下步骤。</p>
    <template #footer>
      <div class="md-bar">
        <button class="ghost" type="button" @click="download">下载 Markdown</button>
        <button class="btn" type="button" :disabled="!(detail?.files || []).length" @click="downloadProject">下载项目</button>
      </div>
      <p v-if="(detail?.files || []).length" class="sub">项目文件：{{ detail.files.join("、") }}</p>
    </template>
  </Dialog>
</template>
