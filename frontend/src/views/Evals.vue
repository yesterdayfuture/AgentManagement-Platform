<script setup>
import { computed, onMounted, ref } from "vue";
import { api, expireSession, session, upload } from "../api";
import { durationText, moneyText } from "../format";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const desk = useDesk("/api/evals/datasets");
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
const runForm = ref({ datasetId: null, name: "", kind: "agent", targetId: "", metrics: ["contains", "completion"] });
const report = ref(null);
const columns = [
  { key: "name", label: "名称" },
  { key: "case_count", label: "用例", format: (value) => value || 0 },
  { key: "created_at", label: "时间", format: when },
];
const caseColumns = [
  { key: "input_text", label: "输入" },
  { key: "expected", label: "期望" },
  { key: "expected_tool", label: "期望工具", format: (value) => value || "—" },
  { key: "created_at", label: "时间", format: when },
];
const targets = computed(() => (runForm.value.kind === "workflow" ? workflows.value : agents.value));

function empty() {
  return { id: null, name: "" };
}
function blankCase() {
  return { id: null, input_text: "", expected: "", expected_tool: "" };
}
function asList(payload) {
  return Array.isArray(payload) ? payload : payload?.items || [];
}
function openWith(row, next) {
  form.value = row ? { id: row.id, name: row.name } : empty();
  draft.value = [];
  draftForm.value = blankCase();
  report.value = null;
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
      await api("/api/evals/datasets", { method: "POST", body: { name: form.value.name, cases: draft.value } });
    } else {
      await api(`/api/evals/datasets/${form.value.id}`, { method: "PATCH", body: { name: form.value.name } });
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
  runForm.value = { datasetId: row.id, name: row.name, kind: "agent", targetId: agents.value[0]?.id || "", metrics: ["contains", "completion"] };
  report.value = null;
  runOpen.value = true;
}
function toggleMetric(key) {
  const list = runForm.value.metrics;
  runForm.value.metrics = list.includes(key) ? list.filter((item) => item !== key) : [...list, key];
}
async function startRun() {
  if (!runForm.value.targetId) {
    desk.error.value = "请选择智能体或工作流";
    return;
  }
  if (!runForm.value.metrics.length) {
    desk.error.value = "请至少选择一个指标";
    return;
  }
  running.value = true;
  desk.error.value = "";
  try {
    const body = { metrics: runForm.value.metrics };
    if (runForm.value.kind === "workflow") body.workflow_id = Number(runForm.value.targetId);
    else body.agent_id = Number(runForm.value.targetId);
    report.value = await api(`/api/evals/datasets/${runForm.value.datasetId}/run`, { method: "POST", body });
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
});
</script>

<template>
  <Desk kicker="09 发布门禁" title="评测" sub="评测集可以逐条添加，也可以按样例上传 Excel。运行前选择智能体或工作流，以及要计算的指标。" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :exact-fields="[{ value: 'name', label: '名称' }]" :error="desk.error.value" :selected="desk.selected.value" @update:selected="desk.selected.value = $event" @load="desk.refresh()" @create="openWith(null, 'create')" @view="openWith($event, 'view')" @edit="openWith($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #extra="{ row }"><button class="ghost" type="button" @click="openRun(row)">运行</button></template>
  </Desk>
  <Dialog wide :open="open && mode !== 'view'" :mode="mode" :title="form.name || '评测集'" @close="open = false" @submit="save">
    <label class="field"><span>名称</span><input v-model="form.name" /></label>
    <template v-if="mode === 'create'">
      <div class="row">
        <button class="ghost" type="button" @click="downloadSample">下载 Excel 样例</button>
        <label class="ghost file-pick">上传 Excel<input type="file" accept=".xlsx" @change="onFile" /></label>
      </div>
      <p class="sub">表头是「输入」「期望」「期望工具」。期望工具可以留空。上传后的行会出现在下面，保存评测集时一起写入。</p>
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
      <Desk kicker="" title="用例" sub="支持分页、每页条数、模糊搜索、精确匹配和时间范围。编辑只改这一条用例。" :columns="caseColumns" :rows="cases.pageData.value.items" :total="cases.pageData.value.total" :query="cases.query" :exact-fields="[{ value: 'input_text', label: '输入' }, { value: 'expected', label: '期望' }, { value: 'expected_tool', label: '期望工具' }]" :error="cases.error.value" :selected="cases.selected.value" create-label="新增用例" @update:selected="cases.selected.value = $event" @load="cases.refresh()" @create="openCase(null, 'create')" @view="openCase($event, 'view')" @edit="openCase($event, 'edit')" @remove="removeCase" @bulk="bulkCases" />
    </template>
  </Dialog>
  <Dialog :open="caseOpen" :mode="caseMode" title="用例" @close="caseOpen = false" @submit="saveCase">
    <label class="field"><span>输入</span><textarea v-model="caseForm.input_text" rows="3" /></label>
    <label class="field"><span>期望</span><textarea v-model="caseForm.expected" rows="3" /></label>
    <label class="field"><span>期望工具</span><input v-model="caseForm.expected_tool" placeholder="可留空" /></label>
  </Dialog>
  <Dialog wide :open="runOpen" mode="run" :title="`运行 ${runForm.name}`" @close="runOpen = false">
    <label class="field"><span>评测对象</span>
      <select v-model="runForm.kind" @change="runForm.targetId = targets[0]?.id || ''">
        <option value="agent">智能体</option>
        <option value="workflow">工作流</option>
      </select>
    </label>
    <label class="field"><span>{{ runForm.kind === "workflow" ? "工作流" : "智能体" }}</span>
      <select v-model="runForm.targetId">
        <option v-for="item in targets" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
    </label>
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
      <div v-if="report" class="sheet" style="margin-top: 14px">
        <strong>{{ report.passed ? "通过" : "未通过" }}</strong>
        <p class="sub">对象 {{ report.target?.name }} · 通过率 {{ Math.round((report.completion || 0) * 100) }}% · 费用 {{ moneyText(report.cost) }}</p>
        <p class="sub" v-for="item in report.metrics || []" :key="item.key">{{ item.name }} {{ item.passed }}/{{ item.total }}</p>
        <article v-for="item in report.details || []" :key="item.id" class="log-line">
          <strong>{{ item.passed ? "通过" : "未过" }} · {{ item.input }}</strong>
          <p class="sub">期望 {{ item.expected }} · {{ durationText(item.latency_ms) }} · {{ moneyText(item.cost) }}<span v-if="item.tools?.length"> · 工具 {{ item.tools.join("、") }}</span></p>
          <p>{{ item.output }}</p>
        </article>
      </div>
    </template>
  </Dialog>
</template>
