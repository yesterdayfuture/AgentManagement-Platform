<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { api, expireSession, session, stream, upload } from "../api";
import { durationText, moneyText } from "../format";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const desk = useDesk("/api/evals/datasets");
const route = useRoute();
const viewing = ref(null);
const cases = useDesk(() => (viewing.value ? `/api/evals/datasets/${viewing.value.id}/cases` : ""));
const agents = ref([]);
const workflows = ref([]);
const metrics = ref([]);
const open = ref(false);
const mode = ref("create");
const form = ref(empty());
const draft = ref([]);
const draftForm = ref(blankCase());
const caseOpen = ref(false);
const caseMode = ref("create");
const caseForm = ref(blankCase());
const runOpen = ref(false);
const running = ref(false);
const runForm = ref({ datasetId: null, name: "", spaceId: null, kind: "agent", targetId: "", agentVersionId: "", minPassRate: 80, metrics: ["contains", "completion"] });
const targetVersions = ref([]);
const historyOpen = ref(false);
const historyDataset = ref(null);
const historyStatus = ref("");
const historyDesk = useDesk(() => historyDataset.value ? `/api/evals/datasets/${historyDataset.value.id}/runs${historyStatus.value === "" ? "" : `?passed=${historyStatus.value}`}` : "");
const noteOpen = ref(false);
const noteForm = ref({ id: null, note: "" });
const detailOpen = ref(false);
const detailReport = ref(null);
const detailQuery = ref({ q: "", status: "", page: 1, page_size: 10 });
const runProgress = ref(null);
const columns = [
  { key: "name", label: "名称" },
  { key: "space_name", label: "所属空间" },
  { key: "case_count", label: "用例", format: (value) => value || 0 },
  { key: "latest_agent", label: "最近评测智能体", format: (value, row) => value ? `${value} · ${row.latest_model || '模型未记录'}` : "—" },
  { key: "created_at", label: "时间", format: when },
];
const caseColumns = [
  { key: "input_text", label: "输入" },
  { key: "expected", label: "期望" },
  { key: "expected_tool", label: "期望工具", format: (value) => value || "—" },
  { key: "created_at", label: "时间", format: when },
];
const historyColumns = [
  { key: "id", label: "编号", format: value => `#${value}` },
  { key: "target", label: "评测对象", format: value => value?.name || "—" },
  { key: "target_version", label: "目标版本", format: (value, row) => `v${value || "—"}${row.target?.kind === "workflow" ? " · 工作流" : " · 智能体"}` },
  { key: "completion", label: "通过率", format: value => `${Math.round((value || 0) * 100)}%` },
  { key: "min_pass_rate", label: "标准", format: value => `${Math.round((value || 1) * 100)}%` },
  { key: "passed", label: "结果", format: value => value ? "通过" : "未通过" },
  { key: "created_at", label: "时间", format: when },
  { key: "note", label: "备注", format: value => value || "—" },
];
const isPlatform = computed(() => Boolean(session().user?.platform_admin));
const canManageHistory = computed(() => isPlatform.value || (session().spaces || []).some(item => item.id === historyDataset.value?.space_id && ["builder", "admin"].includes(item.role)));
const targets = computed(() => (runForm.value.kind === "workflow" ? workflows.value : agents.value).filter(item => isPlatform.value || item.space_id === runForm.value.spaceId));
const filteredDetails = computed(() => (detailReport.value?.details || []).filter(item => {
  const q = detailQuery.value.q.trim().toLocaleLowerCase();
  return (!q || [item.input, item.output, item.expected].some(value => String(value || "").toLocaleLowerCase().includes(q))) && (detailQuery.value.status === "" || item.passed === (detailQuery.value.status === "true"));
}));
const detailPageSize = computed(() => Math.min(100, Math.max(1, Number(detailQuery.value.page_size) || 10)));
const detailPages = computed(() => Math.max(1, Math.ceil(filteredDetails.value.length / detailPageSize.value)));
const visibleDetails = computed(() => filteredDetails.value.slice((detailQuery.value.page - 1) * detailPageSize.value, detailQuery.value.page * detailPageSize.value));
function normalizeDetailSize() { detailQuery.value.page_size = detailPageSize.value; detailQuery.value.page = 1; }

function empty() {
  return { id: null, name: "", source_url: "", license: "", selection_note: "", default_metrics: ["contains", "completion"] };
}
function blankCase() {
  return { id: null, input_text: "", expected: "", expected_tool: "" };
}
function asList(payload) {
  return Array.isArray(payload) ? payload : payload?.items || [];
}
function openWith(row, next) {
  form.value = row ? { id: row.id, name: row.name, source_url: row.source_url || "", license: row.license || "", selection_note: row.selection_note || "", default_metrics: row.default_metrics || ["contains", "completion"] } : empty();
  draft.value = [];
  draftForm.value = blankCase();
  detailReport.value = null;
  mode.value = next;
  viewing.value = next === "view" ? row : null;
  open.value = true;
  if (next === "view") cases.refresh();
}
function addDraft() {
  if (!draftForm.value.input_text.trim() || !draftForm.value.expected.trim()) {
    desk.error.value = "请填写输入和期望";
    return;
  }
  draft.value = [...draft.value, { ...draftForm.value }];
  draftForm.value = blankCase();
  desk.error.value = "";
}
function dropDraft(index) {
  draft.value = draft.value.filter((_, item) => item !== index);
}
async function onFile(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    const parsed = await upload("/api/evals/parse", body);
    draft.value = [...draft.value, ...(parsed.cases || [])];
    desk.error.value = "";
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function downloadSample() {
  const headers = {};
  const current = session();
  if (current.token) headers.Authorization = `Bearer ${current.token}`;
  if (current.spaceId) headers["X-Space-Id"] = current.spaceId;
  const response = await fetch("/api/evals/sample.xlsx", { headers });
  if (!response.ok) {
    if (response.status === 401) expireSession();
    desk.error.value = "样例下载失败";
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "评测用例样例.xlsx";
  link.click();
  URL.revokeObjectURL(url);
}
async function save() {
  try {
    if (mode.value === "create") {
      await api("/api/evals/datasets", { method: "POST", body: { ...form.value, cases: draft.value } });
    } else {
      await api(`/api/evals/datasets/${form.value.id}`, { method: "PATCH", body: { ...form.value } });
    }
    open.value = false;
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
function openCase(row, next) {
  caseForm.value = row ? { ...row } : blankCase();
  caseMode.value = next;
  caseOpen.value = true;
}
async function saveCase() {
  try {
    const body = { input_text: caseForm.value.input_text, expected: caseForm.value.expected, expected_tool: caseForm.value.expected_tool || "" };
    if (caseMode.value === "create") await api(`/api/evals/datasets/${viewing.value.id}/cases`, { method: "POST", body });
    else await api(`/api/evals/cases/${caseForm.value.id}`, { method: "PATCH", body });
    caseOpen.value = false;
    await cases.refresh();
    await desk.refresh();
  } catch (err) {
    cases.error.value = err.message;
  }
}
async function removeCase(row) {
  if (!window.confirm("删除这条用例？")) return;
  try {
    await api(`/api/evals/cases/${row.id}`, { method: "DELETE" });
    await cases.refresh();
    await desk.refresh();
  } catch (err) {
    cases.error.value = err.message;
  }
}
async function bulkCases() {
  if (!window.confirm(`删除所选 ${cases.selected.value.length} 条用例？`)) return;
  try {
    await api(`/api/evals/datasets/${viewing.value.id}/cases/bulk-delete`, { method: "POST", body: { ids: cases.selected.value } });
    await cases.refresh();
    await desk.refresh();
  } catch (err) {
    cases.error.value = err.message;
  }
}
async function remove(row) {
  if (!window.confirm(`删除评测集 ${row.name}？`)) return;
  try { await api(`/api/evals/datasets/${row.id}`, { method: "DELETE" }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 个评测集？`)) return;
  try { await api("/api/evals/datasets/bulk-delete", { method: "POST", body: { ids: desk.selected.value } }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
function openRun(row) {
  const available = agents.value.filter(item => isPlatform.value || item.space_id === row.space_id);
  const requested = available.find(item => item.id === Number(route.query.agent_id));
  const requestedWorkflow = workflows.value.find(item => item.id === Number(route.query.workflow_id));
  runForm.value = { datasetId: row.id, name: row.name, spaceId: row.space_id, kind: requestedWorkflow ? "workflow" : "agent", targetId: requestedWorkflow?.id || requested?.id || available.find(item => item.space_id === row.space_id)?.id || available[0]?.id || "", agentVersionId: "", minPassRate: requestedWorkflow ? Math.round(Number(requestedWorkflow.eval_min_pass_rate || 1) * 100) : 80, metrics: row.default_metrics || ["contains", "completion"] };
  runProgress.value = null;
  historyOpen.value = false;
  runOpen.value = true;
}
async function loadTargetVersions() {
  if (runForm.value.kind !== "agent" || !runForm.value.targetId) { targetVersions.value = []; runForm.value.agentVersionId = ""; return; }
  const id = Number(runForm.value.targetId);
  try {
    const data = await api(`/api/agents/${id}`);
    if (Number(runForm.value.targetId) !== id || runForm.value.kind !== "agent") return;
    targetVersions.value = [...(data.versions || [])].reverse();
    if (!targetVersions.value.some(item => item.id === Number(runForm.value.agentVersionId))) {
      const requested = Number(route.query.agent_id) === id && targetVersions.value.find(item => item.id === Number(route.query.version_id));
      runForm.value.agentVersionId = requested?.id || targetVersions.value[0]?.id || "";
    }
  } catch (err) { desk.error.value = err.message; }
}
watch(() => [runForm.value.kind, runForm.value.targetId], loadTargetVersions);
function changeTargetKind(event) {
  runForm.value.kind = event.target.value;
  runForm.value.targetId = targets.value[0]?.id || "";
}
async function openHistory(row) {
  historyDataset.value = row;
  historyStatus.value = "";
  historyDesk.query.page = 1;
  historyOpen.value = true;
  await historyDesk.refresh();
}
async function showHistorical(item) {
  try {
    detailReport.value = await api(`/api/evals/runs/${item.id}`);
    detailQuery.value = { q: "", status: "", page: 1, page_size: 10 };
    detailOpen.value = true;
  } catch (err) { historyDesk.error.value = err.message; }
}
function editHistoryNote(item) {
  noteForm.value = { id: item.id, note: item.note || "" };
  noteOpen.value = true;
}
async function saveHistoryNote() {
  try {
    await api(`/api/evals/runs/${noteForm.value.id}`, { method: "PATCH", body: { note: noteForm.value.note } });
    noteOpen.value = false;
    await historyDesk.refresh();
  } catch (err) { historyDesk.error.value = err.message; }
}
async function removeHistory(item) {
  if (!window.confirm(`删除评测报告 #${item.id}？删除后无法恢复，相关发布门禁会重新检查。`)) return;
  try { await api(`/api/evals/runs/${item.id}`, { method: "DELETE" }); await historyDesk.refresh(); await desk.refresh(); }
  catch (err) { historyDesk.error.value = err.message; }
}
async function bulkHistory() {
  if (!window.confirm(`删除所选 ${historyDesk.selected.value.length} 份评测报告？删除后无法恢复。`)) return;
  try {
    await api(`/api/evals/datasets/${historyDataset.value.id}/runs/bulk-delete`, { method: "POST", body: { ids: historyDesk.selected.value } });
    await historyDesk.refresh();
    await desk.refresh();
  } catch (err) { historyDesk.error.value = err.message; }
}
function toggleMetric(key) {
  const list = runForm.value.metrics;
  runForm.value.metrics = list.includes(key) ? list.filter((item) => item !== key) : [...list, key];
}
function toggleDefaultMetric(key) {
  const list = form.value.default_metrics || [];
  form.value.default_metrics = list.includes(key) ? list.filter((item) => item !== key) : [...list, key];
}
async function startRun() {
  if (!runForm.value.targetId) {
    desk.error.value = "请选择智能体或工作流";
    return;
  }
  if (runForm.value.kind === "agent" && !runForm.value.agentVersionId) {
    desk.error.value = "请选择要评测的智能体版本";
    return;
  }
  if (!runForm.value.metrics.length) {
    desk.error.value = "请至少选择一个指标";
    return;
  }
  if (!Number.isFinite(Number(runForm.value.minPassRate)) || Number(runForm.value.minPassRate) < 1 || Number(runForm.value.minPassRate) > 100) {
    desk.error.value = "通过标准须在 1% 到 100% 之间";
    return;
  }
  running.value = true;
  desk.error.value = "";
  try {
    const body = { metrics: runForm.value.metrics, min_pass_rate: Number(runForm.value.minPassRate) / 100 };
    if (runForm.value.kind === "workflow") body.workflow_id = Number(runForm.value.targetId);
    else { body.agent_id = Number(runForm.value.targetId); body.agent_version_id = Number(runForm.value.agentVersionId); }
    runProgress.value = { done: 0, total: 0 };
    await stream(`/api/evals/datasets/${runForm.value.datasetId}/run-stream`, body, (event) => {
      if (event.event === "progress") runProgress.value = event;
      if (event.event === "complete") runProgress.value = event.report;
      if (event.event === "error") throw new Error(event.message || "评测失败");
    });
    if (runProgress.value?.id) {
      runOpen.value = false;
      await showHistorical(runProgress.value);
      await desk.refresh();
    }
  } catch (err) {
    desk.error.value = err.message;
  } finally {
    running.value = false;
  }
}
onMounted(async () => {
  agents.value = asList(await api("/api/agents"));
  workflows.value = asList(await api("/api/workflows"));
  metrics.value = await api("/api/evals/metrics");
  await desk.refresh();
  if (route.query.workflow_id) {
    const workflow = workflows.value.find(item => item.id === Number(route.query.workflow_id));
    if (!workflow) desk.error.value = "工作流不存在或无权评测";
    else if (workflow.eval_dataset_id) {
      try { openRun(await api(`/api/evals/datasets/${workflow.eval_dataset_id}`)); }
      catch (err) { desk.error.value = err.message; }
    } else {
      desk.error.value = `「${workflow.name}」尚未指定发布评测集；可点击下方数据集的「运行」并选择该工作流。`;
    }
  }
  if (route.query.report) {
    try {
      await showHistorical({ id: Number(route.query.report) });
    } catch (err) { desk.error.value = err.message; }
  }
});
</script>

<template>
  <Desk kicker="09 发布门禁" title="评测" sub="评测集可以逐条添加，也可以按样例上传 Excel。运行前选择智能体或工作流，以及要计算的指标。" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :exact-fields="[{ value: 'name', label: '名称' }]" :error="desk.error.value" :selected="desk.selected.value" @update:selected="desk.selected.value = $event" @load="desk.refresh()" @create="openWith(null, 'create')" @view="openWith($event, 'view')" @edit="openWith($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #extra="{ row }"><button class="ghost" type="button" @click="openRun(row)">运行</button><button class="ghost" type="button" @click="openHistory(row)">查看历史评测</button></template>
  </Desk>
  <Dialog wide :open="open && mode !== 'view'" :mode="mode" :title="form.name || '评测集'" @close="open = false" @submit="save">
    <label class="field"><span>名称</span><input v-model="form.name" /></label>
    <label class="field"><span>开源数据来源</span><input v-model="form.source_url" placeholder="https://…" /></label>
    <label class="field"><span>许可</span><input v-model="form.license" placeholder="例如 Apache-2.0" /></label>
    <label class="field"><span>选样说明</span><textarea v-model="form.selection_note" rows="2" /></label>
    <div class="field"><span>默认评测指标</span><div class="checks"><label v-for="item in metrics" :key="item.key" class="check"><input type="checkbox" :checked="form.default_metrics.includes(item.key)" @change="toggleDefaultMetric(item.key)" />{{ item.name }}</label></div></div>
    <template v-if="mode === 'create'">
      <div class="row">
        <button class="ghost" type="button" @click="downloadSample">下载 Excel 样例</button>
        <label class="ghost file-pick">上传 Excel / JSON<input type="file" accept=".xlsx,.json" @change="onFile" /></label>
      </div>
      <p class="sub">Excel 表头是「输入」「期望」「期望工具」；也可导入质量评价导出的评估 JSON。上传后的用例会出现在下面，保存评测集时一起写入。</p>
      <label class="field"><span>输入</span><input v-model="draftForm.input_text" /></label>
      <label class="field"><span>期望</span><input v-model="draftForm.expected" /></label>
      <label class="field"><span>期望工具</span><input v-model="draftForm.expected_tool" placeholder="例如 calculator，可留空" /></label>
      <button class="ghost" type="button" @click="addDraft">加入这条</button>
      <article v-for="(item, index) in draft" :key="index" class="log-line">
        <strong>{{ item.input_text }}</strong>
        <p class="sub">期望 {{ item.expected }}<span v-if="item.expected_tool"> · 工具 {{ item.expected_tool }}</span></p>
        <button class="ghost" type="button" @click="dropDraft(index)">移除</button>
      </article>
    </template>
  </Dialog>
  <Dialog wide :open="open && mode === 'view'" mode="view" :title="viewing?.name || '用例'" @close="open = false">
    <template #footer>
      <p v-if="viewing?.source_url" class="sub">来源：<a :href="viewing.source_url" target="_blank" rel="noopener noreferrer">{{ viewing.source_url }}</a> · {{ viewing.license || '许可未注明' }}</p>
      <p v-if="viewing?.selection_note" class="sub">选样：{{ viewing.selection_note }}</p>
      <Desk kicker="" title="用例" sub="支持分页、每页条数、模糊搜索、精确匹配和时间范围。编辑只改这一条用例。" :columns="caseColumns" :rows="cases.pageData.value.items" :total="cases.pageData.value.total" :query="cases.query" :exact-fields="[{ value: 'input_text', label: '输入' }, { value: 'expected', label: '期望' }, { value: 'expected_tool', label: '期望工具' }]" :error="cases.error.value" :selected="cases.selected.value" create-label="新增用例" @update:selected="cases.selected.value = $event" @load="cases.refresh()" @create="openCase(null, 'create')" @view="openCase($event, 'view')" @edit="openCase($event, 'edit')" @remove="removeCase" @bulk="bulkCases" />
    </template>
  </Dialog>
  <Dialog :open="caseOpen" :mode="caseMode" title="用例" @close="caseOpen = false" @submit="saveCase">
    <label class="field"><span>输入</span><textarea v-model="caseForm.input_text" rows="3" /></label>
    <label class="field"><span>期望</span><textarea v-model="caseForm.expected" rows="3" /></label>
    <label class="field"><span>期望工具</span><input v-model="caseForm.expected_tool" placeholder="可留空" /></label>
  </Dialog>
  <Dialog wide :open="historyOpen" mode="view" :title="`${historyDataset?.name || '数据集'} · 历史评测`" @close="historyOpen = false">
    <template #footer>
      <p class="sub">创建新评测会生成一份不可变报告；编辑仅修改备注，不改变题目、模型输出和评分。历史评测按最新时间排序。</p>
      <Desk kicker="" title="历史评测" sub="可按对象名称、报告编号或备注查询；支持时间范围、状态筛选、分页和自定义每页条数。" :columns="historyColumns" :rows="historyDesk.pageData.value.items" :total="historyDesk.pageData.value.total" :query="historyDesk.query" :exact-fields="[{ value: 'id', label: '报告编号' }, { value: 'agent_id', label: '智能体编号' }, { value: 'workflow_id', label: '工作流编号' }, { value: 'workflow_revision', label: '工作流版本' }]" :error="historyDesk.error.value" :selected="historyDesk.selected.value" :actions-width="190" :creatable="canManageHistory" :editable="canManageHistory" :removable="canManageHistory" create-label="新增评测" @update:selected="historyDesk.selected.value = $event" @load="historyDesk.refresh()" @create="openRun(historyDataset)" @view="showHistorical" @edit="editHistoryNote" @remove="removeHistory" @bulk="bulkHistory">
        <template #filters><label>结果<SearchSelect v-model="historyStatus" @change="historyDesk.query.page = 1; historyDesk.refresh()"><option value="">全部</option><option value="true">通过</option><option value="false">未通过</option></SearchSelect></label></template>
      </Desk>
    </template>
  </Dialog>
  <Dialog nested :open="noteOpen" mode="edit" title="编辑评测备注" @close="noteOpen = false" @submit="saveHistoryNote">
    <p class="sub">报告输出与判分作为历史快照保留；此处只编辑备注。</p>
    <label class="field"><span>备注</span><textarea v-model="noteForm.note" rows="4" maxlength="1000" placeholder="例如：额度耗尽后重跑、人工复核结论" /></label>
  </Dialog>
  <Dialog nested wide :open="detailOpen" mode="view" :title="`评测报告 #${detailReport?.id || '—'}`" @close="detailOpen = false">
    <template #footer>
      <div v-if="detailReport" class="eval-report-summary">
        <strong :class="detailReport.passed ? 'eval-passed' : 'eval-failed'">{{ detailReport.passed ? '通过' : '未通过' }}</strong>
        <span>{{ detailReport.dataset_name }} · {{ detailReport.target?.name }} {{ detailReport.target?.kind === 'workflow' ? '工作流' : '智能体' }} v{{ detailReport.target_version || '—' }}</span>
        <span>通过率 {{ Math.round((detailReport.completion || 0) * 100) }}% / 标准 {{ Math.round((detailReport.min_pass_rate || 1) * 100) }}%</span>
        <span>模型 {{ detailReport.agent?.model_code || detailReport.agent?.model || '—' }} · {{ moneyText(detailReport.cost) }}</span>
      </div>
      <p v-if="detailReport?.note" class="sub">备注：{{ detailReport.note }}</p>
      <p class="sub" v-for="item in detailReport?.metrics || []" :key="item.key">{{ item.name }} {{ item.passed }}/{{ item.total }}</p>
      <div class="row filters eval-detail-filters">
        <label>题目或答案<input v-model="detailQuery.q" placeholder="搜索输入、输出或真实答案" @input="detailQuery.page = 1" /></label>
        <label>判定<SearchSelect v-model="detailQuery.status" @change="detailQuery.page = 1"><option value="">全部</option><option value="true">通过</option><option value="false">未通过</option></SearchSelect></label>
        <label>每页<input v-model.number="detailQuery.page_size" type="number" min="1" max="100" @change="normalizeDetailSize" /></label>
      </div>
      <div class="eval-detail-list">
        <article v-for="item in visibleDetails" :key="item.id" class="eval-case-card">
          <div class="eval-case-head"><strong :class="item.passed ? 'eval-passed' : 'eval-failed'">{{ item.passed ? '通过' : '未通过' }}</strong><span>题目 #{{ item.id }} · {{ durationText(item.latency_ms) }} · {{ moneyText(item.cost) }}</span></div>
          <div class="eval-case-grid"><section><small>问题输入</small><pre>{{ item.input }}</pre></section><section><small>模型输出</small><pre>{{ item.output || '—' }}</pre></section><section><small>真实答案</small><pre>{{ item.expected }}</pre></section></div>
          <p class="sub">{{ item.model_online ? '模型在线' : '模型未在线响应' }}<span v-if="item.tools?.length"> · 调用工具 {{ item.tools.join('、') }}</span><span v-if="item.expected_tool"> · 期望工具 {{ item.expected_tool }}</span></p>
          <p class="sub">指标：{{ Object.entries(item.scores || {}).map(([key, value]) => `${key} ${value ? '✓' : '×'}`).join(' · ') }}</p>
          <p v-if="item.run_id" class="sub"><router-link :to="`/workflow-runs?run_id=${item.run_id}`">查看工作流执行记录</router-link></p>
        </article>
        <p v-if="!visibleDetails.length" class="sub">没有匹配的问题。</p>
      </div>
      <div class="pager"><span>共 {{ filteredDetails.length }} 题</span><div class="row"><button class="ghost" type="button" :disabled="detailQuery.page <= 1" @click="detailQuery.page--">上一页</button><span>{{ detailQuery.page }} / {{ detailPages }}</span><button class="ghost" type="button" :disabled="detailQuery.page >= detailPages" @click="detailQuery.page++">下一页</button></div></div>
    </template>
  </Dialog>
  <Dialog wide :open="runOpen" mode="run" :title="`运行 ${runForm.name}`" @close="runOpen = false">
    <label class="field"><span>评测对象</span>
      <SearchSelect v-model="runForm.kind" @change="changeTargetKind">
        <option value="agent">智能体</option>
        <option value="workflow">工作流</option>
      </SearchSelect>
    </label>
    <label class="field"><span>{{ runForm.kind === "workflow" ? "工作流" : "智能体" }}</span>
      <SearchSelect v-model="runForm.targetId">
        <option v-for="item in targets" :key="item.id" :value="item.id">{{ item.name }} · {{ item.space_id == null ? '平台 · 未分配空间' : item.department || '所属空间' }}</option>
      </SearchSelect>
    </label>
    <p class="sub">当前数据集位于「{{ desk.pageData.value.items.find(item => item.id === runForm.datasetId)?.space_name || '所属空间' }}」。{{ isPlatform ? '平台管理员可选择所有可评测对象；未分配空间的智能体在数据集空间执行，跨空间及未分配空间的报告仅平台管理员可见。' : '只显示当前数据集所属空间内、你有权限评测的对象。' }} 共 {{ targets.length }} 个候选对象。</p>
    <label v-if="runForm.kind === 'agent'" class="field"><span>评测智能体版本</span><SearchSelect v-model="runForm.agentVersionId"><option v-for="item in targetVersions" :key="item.id" :value="item.id">v{{ item.number }} · {{ item.status }} · {{ item.model_name || '按任务选择' }}{{ item.number === targetVersions[0]?.number ? '（最新）' : '' }}</option></SearchSelect></label>
    <p v-if="runForm.kind === 'workflow'" class="sub">评估工作流当前版本 v{{ workflows.find(item => item.id === Number(runForm.targetId))?.revision || '—' }}，报告将固定记录该版本。</p>
    <label class="field"><span>通过标准（通过用例占比，%）</span><input v-model.number="runForm.minPassRate" type="number" min="1" max="100" step="1" /></label>
    <p class="sub">单条用例须满足下方勾选的全部指标；通过用例占比达到此比例，整次评测才算通过。工作流发布还须满足其发布门禁比例。</p>
    <p class="sub">评估指标。任务完成、精确匹配、工具命中、耗时、费用和依据忠实来自智能体评测里常用的可计算指标。</p>
    <div class="checks">
      <label v-for="item in metrics" :key="item.key" class="check">
        <input type="checkbox" :checked="runForm.metrics.includes(item.key)" @change="toggleMetric(item.key)" />
        {{ item.name }}
        <span class="sub">{{ item.summary }}</span>
      </label>
    </div>
    <template #footer>
      <button class="btn" type="button" :disabled="running" @click="startRun">{{ running ? "评测中" : "开始评测" }}</button>
      <p v-if="running && runProgress" class="sub">已完成 {{ runProgress.done }}/{{ runProgress.total || '…' }} 条；最近一条{{ runProgress.model_online ? '真实模型响应' : '模型回退或未响应' }}</p>
    </template>
  </Dialog>
</template>
<style scoped>
.eval-report-summary { display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center; padding: 14px 0; border-bottom: 1px solid var(--line); font-size: 12px; }
.eval-report-summary span { color: var(--muted); }
.eval-passed { color: #168264; }
.eval-failed { color: #c54b5b; }
.eval-detail-filters { margin: 18px 0 8px; }
.eval-detail-list { display: grid; gap: 12px; max-height: 58vh; overflow-y: auto; padding: 4px 2px; }
.eval-case-card { padding: 16px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface); }
.eval-case-head { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; }
.eval-case-head span { color: var(--muted); }
.eval-case-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }
.eval-case-grid section { min-width: 0; padding: 12px; border-radius: 7px; background: #f7f9fc; }
.eval-case-grid small { display: block; margin-bottom: 6px; color: var(--muted); }
.eval-case-grid pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.5; }
@media (max-width: 900px) { .eval-case-grid { grid-template-columns: 1fr; } }
</style>
