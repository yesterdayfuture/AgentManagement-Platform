<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, session, stream } from "../api";
import { countText, moneyText, prettyJson } from "../format";
import { useDesk, when, withScope, departmentNote } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const router = useRouter();
const route = useRoute();
const desk = useDesk(() => withScope(route, "/api/workflows"));
const agents = ref([]);
const tools = ref([]);
const bases = ref([]);
const mcpServers = ref([]);
const skills = ref([]);
const open = ref(false);
const mode = ref("create");
const form = ref(blank());
const selectedId = ref("");
const runMessage = ref("写一篇目前agent技术调研报告");
const testStream = ref(false);
const runOutput = ref("");
const liveLogs = ref([]);
const running = ref(false);
const runOpen = ref(false);
const runForm = ref(null);
const draft = ref(null);
const estimate = ref(null);
const confirmOpen = ref(false);
const pendingAction = ref("");
const platform = computed(() => Boolean(session().user?.platform_admin));
const columns = computed(() => [
  { key: "name", label: "名称" },
  { key: "release", label: "发布", format: (value) => (value === "published" ? "已发布" : "草稿") },
  { key: "enabled", label: "启用", format: (value) => (value ? "启用" : "停用") },
  { key: "node_count", label: "节点" },
  { key: "schedule_enabled", label: "定时", format: (_value, row) => (row.schedule_enabled ? `每 ${row.schedule_minutes} 分钟` : "—") },
  { key: "latest_status", label: "最近执行", format: (value) => statusText(value) },
  ...(platform.value
    ? [
        { key: "creator", label: "创建者", format: (value) => value || "—" },
        { key: "department", label: "部门" },
      ]
    : []),
  { key: "created_at", label: "时间", format: when },
]);

function blank() {
  return { id: null, name: "", enabled: true, release: "draft", mode: "agents", legacy: false, nodes: [], edges: [] };
}
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
function agentName(id) {
  return agents.value.find((item) => item.id === Number(id))?.name || "智能体";
}
function asList(payload) {
  return Array.isArray(payload) ? payload : payload?.items || [];
}
function fromRow(row) {
  const definition = row.definition || {};
  const agentNodes = (definition.nodes || []).filter((node) => node.type === "agent");
  return {
    id: row.id,
    name: row.name,
    enabled: row.enabled !== false,
    release: row.release || "draft",
    mode: definition.kind === "autonomous" ? "autonomous" : "agents",
    legacy: !agentNodes.length && (definition.nodes || []).length > 0,
    nodes: agentNodes.map((node, index) => ({
      id: node.id,
      agent_id: node.agent_id,
      x: Number.isFinite(node.x) ? node.x : 36 + (index % 4) * 180,
      y: Number.isFinite(node.y) ? node.y : 36 + Math.floor(index / 4) * 120,
      prompt: node.prompt ?? null,
      tool_names: Array.isArray(node.tool_names) ? [...node.tool_names] : null,
      knowledge_ids: Array.isArray(node.knowledge_ids) ? [...node.knowledge_ids] : null,
      mcp_ids: Array.isArray(node.mcp_ids) ? [...node.mcp_ids] : null,
      skill_ids: Array.isArray(node.skill_ids) ? [...node.skill_ids] : null,
    })),
    edges: agentNodes.length ? (definition.edges || []).map((edge) => ({ src: edge.src, dst: edge.dst })) : [],
  };
}
function openWith(row, next) {
  form.value = row ? fromRow(row) : blank();
  runOutput.value = "";
  liveLogs.value = [];
  draft.value = null;
  selectedId.value = "";
  mode.value = next;
  open.value = true;
}
function addAgent(agent, x = 36 + form.value.nodes.length * 28, y = 36 + form.value.nodes.length * 24) {
  const used = new Set(form.value.nodes.map((node) => node.id));
  let index = form.value.nodes.length + 1;
  while (used.has(`n${index}`)) index += 1;
  form.value.nodes.push({ id: `n${index}`, agent_id: agent.id, x, y, prompt: null, tool_names: null, knowledge_ids: null, mcp_ids: null, skill_ids: null });
}
async function fillNode(node) {
  if (Array.isArray(node.tool_names) && Array.isArray(node.knowledge_ids) && Array.isArray(node.mcp_ids) && Array.isArray(node.skill_ids) && node.prompt != null) return;
  const detail = await api(`/api/agents/${node.agent_id}`);
  const latest = detail.versions?.at(-1);
  if (node.prompt == null) node.prompt = latest?.system_prompt || "";
  if (!Array.isArray(node.tool_names)) node.tool_names = [...(latest?.tools || [])];
  if (!Array.isArray(node.knowledge_ids)) node.knowledge_ids = bases.value.map((item) => item.id);
  if (!Array.isArray(node.mcp_ids)) node.mcp_ids = [...(latest?.mcp_ids || [])];
  if (!Array.isArray(node.skill_ids)) node.skill_ids = [...(latest?.skill_ids || [])];
}
async function configure(node) {
  selectedId.value = node.id;
  await fillNode(node);
}
const selected = computed(() => form.value.nodes.find((node) => node.id === selectedId.value) || null);
function toggleName(list, name) {
  return list.includes(name) ? list.filter((item) => item !== name) : [...list, name];
}
function toggleId(list, id) {
  return list.includes(id) ? list.filter((item) => item !== id) : [...list, id];
}
function onDragStart(event, agent) {
  event.dataTransfer.setData("agent", String(agent.id));
}
function onDrop(event) {
  const agent = agents.value.find((item) => item.id === Number(event.dataTransfer.getData("agent")));
  if (!agent || mode.value === "view") return;
  const rect = event.currentTarget.getBoundingClientRect();
  addAgent(agent, Math.max(12, event.clientX - rect.left - 74), Math.max(12, event.clientY - rect.top - 28));
}
function startMove(node, event) {
  if (mode.value === "view" || event.target.classList?.contains("port")) return;
  const origin = { x: event.clientX, y: event.clientY, nx: node.x, ny: node.y };
  const move = (ev) => {
    node.x = Math.max(8, origin.nx + ev.clientX - origin.x);
    node.y = Math.max(8, origin.ny + ev.clientY - origin.y);
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}
function connect(src, dst) {
  if (!src || !dst || src === dst) return;
  if (form.value.edges.some((edge) => edge.src === src && edge.dst === dst)) return;
  form.value.edges.push({ src, dst });
}
function onWireDrag(event, node) {
  event.dataTransfer.setData("wire", node.id);
  event.stopPropagation();
}
function onWireDrop(event, node) {
  const src = event.dataTransfer.getData("wire");
  if (src) connect(src, node.id);
}
function startWire(node, event) {
  if (mode.value === "view") return;
  const board = event.currentTarget.closest(".board");
  const rect = board.getBoundingClientRect();
  const origin = { x: node.x + 148, y: node.y + 28 };
  const move = (ev) => {
    draft.value = { src: node.id, x1: origin.x, y1: origin.y, x2: ev.clientX - rect.left, y2: ev.clientY - rect.top };
  };
  const up = (ev) => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    const hit = document.elementFromPoint(ev.clientX, ev.clientY);
    const inlet = hit?.closest?.("[data-inlet]");
    if (inlet) connect(node.id, inlet.dataset.inlet);
    draft.value = null;
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}
function removeNode(id) {
  form.value.nodes = form.value.nodes.filter((node) => node.id !== id);
  form.value.edges = form.value.edges.filter((edge) => edge.src !== id && edge.dst !== id);
}
const leadId = computed(() => {
  const incoming = new Set(form.value.edges.map((edge) => edge.dst));
  return form.value.nodes.find((node) => !incoming.has(node.id))?.id || form.value.nodes[0]?.id || "";
});
const lines = computed(() =>
  form.value.edges
    .map((edge) => {
      const src = form.value.nodes.find((node) => node.id === edge.src);
      const dst = form.value.nodes.find((node) => node.id === edge.dst);
      if (!src || !dst) return null;
      return { src: edge.src, dst: edge.dst, x1: src.x + 148, y1: src.y + 28, x2: dst.x, y2: dst.y + 28 };
    })
    .filter(Boolean),
);
function definitionOf() {
  if (!form.value.nodes.length) throw new Error("把智能体拖进画布，至少一个");
  return {
    kind: form.value.mode === "autonomous" ? "autonomous" : "agents",
    entry: form.value.nodes[0].id,
    nodes: form.value.nodes.map((node) => ({
      id: node.id,
      type: "agent",
      agent_id: Number(node.agent_id),
      x: Math.round(node.x),
      y: Math.round(node.y),
      ...(node.prompt ? { prompt: node.prompt } : {}),
      ...(Array.isArray(node.tool_names) ? { tool_names: node.tool_names } : {}),
      ...(Array.isArray(node.knowledge_ids) ? { knowledge_ids: node.knowledge_ids } : {}),
      ...(Array.isArray(node.mcp_ids) ? { mcp_ids: node.mcp_ids } : {}),
      ...(Array.isArray(node.skill_ids) ? { skill_ids: node.skill_ids } : {}),
    })),
    edges: form.value.edges.map((edge) => ({ src: edge.src, dst: edge.dst })),
  };
}
async function save(options = {}) {
  desk.error.value = "";
  try {
    if (!form.value.name.trim()) throw new Error("请填写名称");
    const definition = definitionOf();
    const payload = {
      name: form.value.name.trim(),
      agent_id: definition.nodes[0].agent_id,
      enabled: form.value.enabled,
      definition,
    };
    if (mode.value === "create" && !form.value.id) {
      const created = await api("/api/workflows", { method: "POST", body: payload });
      form.value.id = created.id;
      mode.value = "edit";
    } else {
      await api(`/api/workflows/${form.value.id}`, { method: "PATCH", body: payload });
    }
    if (!options.keep) open.value = false;
    await desk.refresh();
    return true;
  } catch (err) {
    desk.error.value = err.message;
    return false;
  }
}
async function remove(row) {
  if (!window.confirm(`删除工作流 ${row.name}？`)) return;
  try {
    await api(`/api/workflows/${row.id}`, { method: "DELETE" });
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条？`)) return;
  try {
    await api("/api/workflows/bulk-delete", { method: "POST", body: { ids: desk.selected.value } });
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
function nodePayload(node) {
  return {
    agent_id: Number(node.agent_id),
    node_id: node.id,
    prompt: node.prompt || "",
    tool_names: node.tool_names || [],
    knowledge_ids: node.knowledge_ids || [],
    mcp_ids: node.mcp_ids || [],
    skill_ids: node.skill_ids || [],
  };
}
async function play(id, message, nodes, wantStream = false) {
  running.value = true;
  liveLogs.value = [];
  runOutput.value = "";
  try {
    if (wantStream) {
      await stream(`/api/workflows/${id}/run`, { message, environment: "test", stream: true, nodes }, (event) => {
        if (event.event === "delta") {
          runOutput.value += event.text || "";
          return;
        }
        if (event.event === "reset") {
          runOutput.value = "";
          return;
        }
        if (event.event === "完成") {
          runOutput.value = event.output || runOutput.value;
        }
        if (event.event === "错误") {
          runOutput.value = event.output || "执行失败";
        }
        if (event.event !== "delta" && event.event !== "reset") {
          liveLogs.value = [...liveLogs.value, event];
        }
      });
    } else {
      const result = await api(`/api/workflows/${id}/run`, {
        method: "POST",
        body: { message, environment: "test", stream: false, nodes },
      });
      runOutput.value = result.output || "";
      liveLogs.value = [
        {
          event: "完成",
          status: result.status,
          output: result.output,
          run_id: result.run_id,
          duration_ms: result.duration_ms,
          input: message,
          agent: "",
          node: "",
          ms: result.duration_ms,
        },
      ];
    }
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
    runOutput.value = err.message;
  } finally {
    running.value = false;
  }
}
const placeText = {
  start: "你是这条固定流程的起点。阅读本次任务，整理后续节点要用的要点，不要抢写最终成稿。",
  middle: "你在固定流程的中间。以上一步的输出为输入，只完成本节点的职责，把结果写清楚交给下一步。",
  end: "你是这条固定流程的最后一步。综合本次任务和前面的结果，给出可以直接交给用户的最终回答。",
  lead: "你是自主调度的调度者。阅读本次任务和各成员职责，决定拆成哪些子任务、由谁完成。",
  member: "你是可被调度的成员。如果子任务交给你，只完成交给你的部分，并写明依据。",
};
function placeOf(node, form) {
  const incoming = new Set((form.edges || []).map((edge) => edge.dst));
  const outgoing = new Set((form.edges || []).map((edge) => edge.src));
  if (form.mode === "autonomous") {
    const lead = form.nodes.find((item) => !incoming.has(item.id)) || form.nodes[0];
    return node.id === lead?.id ? "lead" : "member";
  }
  if (!incoming.has(node.id)) return "start";
  if (!outgoing.has(node.id)) return "end";
  return "middle";
}
function composePrompt(base, task, place) {
  const duty = (base || "").trim();
  const question = (task || "").trim();
  if (!question) return duty;
  return [duty, `本次任务：${question}`, placeText[place] || ""].filter(Boolean).join("\n\n");
}
function applyTaskPrompts() {
  if (!runForm.value) return;
  for (const node of runForm.value.nodes) {
    node.prompt = composePrompt(node.basePrompt, runForm.value.message, placeOf(node, runForm.value));
  }
}
async function openRun(row) {
  const definition = row.definition || {};
  const nodes = (definition.nodes || []).filter((node) => node.type === "agent").map((node) => ({
    id: node.id,
    agent_id: node.agent_id,
    prompt: node.prompt ?? null,
    basePrompt: "",
    tool_names: Array.isArray(node.tool_names) ? [...node.tool_names] : null,
    knowledge_ids: Array.isArray(node.knowledge_ids) ? [...node.knowledge_ids] : null,
    mcp_ids: Array.isArray(node.mcp_ids) ? [...node.mcp_ids] : null,
    skill_ids: Array.isArray(node.skill_ids) ? [...node.skill_ids] : null,
  }));
  const taskFromQuery = String(route.query.task || "").trim();
  runForm.value = {
    id: row.id,
    name: row.name,
    release: row.release || "draft",
    mode: definition.kind === "autonomous" ? "autonomous" : "agents",
    filling: true,
    stream: false,
    scheduled: Boolean(row.schedule_enabled),
    everyMinutes: row.schedule_minutes || 60,
    nextAt: row.schedule_next_at || "",
    tokenBudget: row.schedule_token_budget || 0,
    tokenUsed: row.schedule_token_used || 0,
    message: taskFromQuery || (row.schedule_enabled && row.schedule_message ? row.schedule_message : runMessage.value),
    edges: (definition.edges || []).map((edge) => ({ src: edge.src, dst: edge.dst })),
    nodes,
  };
  liveLogs.value = [];
  runOpen.value = true;
  try {
    for (const node of runForm.value.nodes) {
      await fillNode(node);
      node.basePrompt = node.prompt || "";
    }
    runForm.value.filling = false;
    applyTaskPrompts();
  } catch (err) {
    if (runForm.value) runForm.value.filling = false;
    desk.error.value = err.message;
  }
}
watch(() => runForm.value?.message, () => {
  if (!runForm.value || runForm.value.filling) return;
  applyTaskPrompts();
});
async function saveSchedule(enabled) {
  const form = runForm.value;
  const saved = await api(`/api/workflows/${form.id}`, {
    method: "PATCH",
    body: {
      schedule_enabled: enabled,
      schedule_minutes: Math.max(1, Number(form.everyMinutes) || 60),
      schedule_message: enabled ? form.message.trim() : "",
      schedule_nodes: form.nodes.map(nodePayload),
      schedule_token_budget: Math.max(0, Number(form.tokenBudget) || 0),
    },
  });
  form.scheduled = Boolean(saved.schedule_enabled);
  form.everyMinutes = saved.schedule_minutes || form.everyMinutes;
  form.nextAt = saved.schedule_next_at || "";
  form.tokenBudget = saved.schedule_token_budget || 0;
  form.tokenUsed = saved.schedule_token_used || 0;
  await desk.refresh();
  return saved;
}
async function prepareConfirm(action) {
  if (!runForm.value?.message?.trim()) {
    desk.error.value = "请填写问题";
    return;
  }
  const minutes = Number(runForm.value.everyMinutes);
  if (runForm.value.scheduled && (!Number.isFinite(minutes) || minutes < 1)) {
    desk.error.value = "定时间隔至少 1 分钟";
    return;
  }
  desk.error.value = "";
  try {
    estimate.value = await api(`/api/workflows/${runForm.value.id}/run-estimate`);
    pendingAction.value = action;
    confirmOpen.value = true;
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function startRun() {
  await prepareConfirm(runForm.value?.scheduled ? "schedule" : "run");
}
async function confirmRun() {
  confirmOpen.value = false;
  const action = pendingAction.value;
  pendingAction.value = "";
  if (action === "schedule") {
    try {
      const saved = await saveSchedule(true);
      runOutput.value = `已按每 ${saved.schedule_minutes} 分钟执行，下次 ${saved.schedule_next_at || "稍后"}`;
    } catch (err) {
      desk.error.value = err.message;
    }
    return;
  }
  try {
    if (runForm.value.nextAt) await saveSchedule(false);
  } catch (err) {
    desk.error.value = err.message;
    return;
  }
  await play(runForm.value.id, runForm.value.message.trim(), runForm.value.nodes.map(nodePayload), Boolean(runForm.value.stream));
}
async function publish(row) {
  try {
    await api(`/api/workflows/${row.id}/publish`, { method: "POST" });
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function control(row, action) {
  desk.error.value = "";
  try {
    const result = await api(`/api/workflows/${row.id}/${action}`, { method: "POST" });
    runOutput.value = `${statusText(result.status)} · ${result.output || ""}`.trim();
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function toggleEnabled(row) {
  try {
    await api(`/api/workflows/${row.id}`, { method: "PATCH", body: { enabled: !row.enabled } });
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function testFlow() {
  runOutput.value = "";
  if (mode.value !== "view") {
    const saved = await save({ keep: true });
    if (!saved) return;
  }
  if (!form.value.id) return;
  try {
    const nodes = [];
    for (const node of form.value.nodes) {
      await fillNode(node);
      nodes.push(nodePayload(node));
    }
    await play(form.value.id, runMessage.value, nodes, Boolean(testStream.value));
  } catch (err) {
    runOutput.value = err.message;
  }
}
onMounted(async () => {
  try {
    agents.value = asList(await api("/api/agents"));
    tools.value = asList(await api("/api/tools?page=1&page_size=100"));
    bases.value = asList(await api("/api/knowledge?page=1&page_size=100"));
    mcpServers.value = asList(await api("/api/mcp?page=1&page_size=100"));
    skills.value = asList(await api("/api/skills?page=1&page_size=100"));
  } catch (err) {
    desk.error.value = err.message;
  }
  await desk.refresh();
  const openId = Number(route.query.open || 0);
  if (openId) {
    const row = desk.pageData.value.items.find((item) => item.id === openId);
    if (row) openRun(row);
    else {
      try {
        const detail = await api(`/api/workflows/${openId}`);
        openRun(detail);
      } catch (err) {
        desk.error.value = err.message;
      }
    }
  }
});
watch(() => route.query.usable_space, () => desk.refresh());
</script>

<template>
  <Desk
    :actions-width="500"
    kicker="06 编排"
    title="工作流"
    :sub="`拖动节点连线。点节点上的「配置」可写这个节点的提示词、工具和知识库。执行时还可以按这次运行再改，并看到每个节点的输入输出。${departmentNote(route)}`"
    create-label="新建工作流"
    empty-text="还没有工作流。点右上角「新建工作流」。"
    :columns="columns"
    :rows="desk.pageData.value.items"
    :total="desk.pageData.value.total"
    :query="desk.query"
    :exact-fields="[{ value: 'name', label: '名称' }, { value: 'status', label: '状态' }, { value: 'release', label: '发布' }]"
    :error="desk.error.value"
    :selected="desk.selected.value"
    @update:selected="desk.selected.value = $event"
    @load="desk.refresh()"
    @create="openWith(null, 'create')"
    @view="openWith($event, 'view')"
    @edit="openWith($event, 'edit')"
    @remove="remove"
    @bulk="bulk"
  >
    <template #extra="{ row }">
      <button class="ghost" type="button" @click="openRun(row)">执行</button>
      <button v-if="row.release !== 'published'" class="ghost" type="button" @click="publish(row)">发布</button>
      <button class="ghost" type="button" @click="control(row, 'pause')">暂停</button>
      <button class="ghost" type="button" @click="control(row, 'stop')">停止</button>
      <button class="ghost" type="button" @click="control(row, 'resume')">继续</button>
      <button class="ghost" type="button" @click="toggleEnabled(row)">{{ row.enabled ? "停用" : "启用" }}</button>
      <button class="ghost" type="button" @click="router.push(`/workflow-runs?workflow_id=${row.id}`)">结果</button>
    </template>
  </Desk>
  <p v-if="runOutput && !open" class="sub">{{ runOutput }}</p>
  <Dialog wide :open="open" :mode="mode" :title="form.name || '新建工作流'" @close="open = false" @submit="save()">
    <label class="field"><span>名称</span><input v-model="form.name" placeholder="例如 客服转退款" /></label>
    <label class="field"><span>执行方式</span>
      <select v-model="form.mode">
        <option value="agents">固定步骤：按连线依次流转</option>
        <option value="autonomous">自主调度：先拆子任务，再自己做或分发</option>
      </select>
    </label>
    <label class="check"><input type="checkbox" v-model="form.enabled" /> 启用。停用后不能执行，也不能继续。</label>
    <p class="sub">当前为{{ form.release === "published" ? "已发布" : "草稿" }}。改节点保存后会回到草稿，需再点「发布」后操作者才能执行。</p>
    <p v-if="form.mode === 'autonomous'" class="sub">调度者会阅读问题，以及每个节点上写的提示词和职责，再决定拆开交给谁。</p>
    <p v-if="form.legacy" class="sub">这份工作流还是旧的意图节点。拖入智能体并保存后，改为智能体之间流转。</p>
    <div class="flow">
      <div class="palette">
        <p class="kicker">智能体</p>
        <button v-for="agent in agents" :key="agent.id" type="button" draggable="true" @dragstart="onDragStart($event, agent)" @click="addAgent(agent)">
          {{ agent.name }}
        </button>
        <p v-if="!agents.length" class="sub">还没有智能体。</p>
      </div>
      <div class="board" @dragover.prevent @drop="onDrop">
        <svg class="wires">
          <line v-for="line in lines" :key="`${line.src}-${line.dst}`" :x1="line.x1" :y1="line.y1" :x2="line.x2" :y2="line.y2" @click="mode !== 'view' && (form.edges = form.edges.filter((edge) => edge.src !== line.src || edge.dst !== line.dst))" />
          <line v-if="draft" class="wire-draft" :x1="draft.x1" :y1="draft.y1" :x2="draft.x2" :y2="draft.y2" />
        </svg>
        <article v-for="node in form.nodes" :key="node.id" class="flow-node" :class="{ lead: node.id === leadId }" :style="{ left: `${node.x}px`, top: `${node.y}px` }" :data-inlet="node.id" @pointerdown="startMove(node, $event)" @dragover.prevent @drop="onWireDrop($event, node)">
          <button class="port in" type="button" aria-label="连入" :data-inlet="node.id" @dragover.prevent @drop.stop="onWireDrop($event, node)" />
          <header>{{ agentName(node.agent_id) }}</header>
          <p class="sub">{{ node.id === leadId ? (form.mode === "autonomous" ? "调度" : "起点") : "下游" }}</p>
          <button class="ghost" type="button" @click.stop="configure(node)">配置</button>
          <button class="ghost" type="button" @click.stop="removeNode(node.id)">去掉</button>
          <button class="port out" type="button" draggable="true" aria-label="拖出连线" @dragstart.stop="onWireDrag($event, node)" @pointerdown.stop="startWire(node, $event)" />
        </article>
      </div>
    </div>
    <p v-for="(edge, index) in form.edges" :key="`${edge.src}-${edge.dst}`" class="sub">
      {{ edge.src }} → {{ edge.dst }}
      <button class="ghost" type="button" @click="form.edges.splice(index, 1)">去掉</button>
    </p>
    <section v-if="selected" class="node-setup">
      <p class="kicker">{{ agentName(selected.agent_id) }}</p>
      <label class="field"><span>提示词</span><textarea v-model="selected.prompt" rows="3" placeholder="留空则使用智能体自己的提示词" /></label>
      <p class="sub">工具</p>
      <div class="checks">
        <label v-for="tool in tools" :key="tool.id" class="check"><input type="checkbox" :checked="selected.tool_names?.includes(tool.name)" @change="selected.tool_names = toggleName(selected.tool_names || [], tool.name)" /> {{ tool.name }}</label>
      </div>
      <p class="sub">知识库</p>
      <div class="checks">
        <label v-for="base in bases" :key="base.id" class="check"><input type="checkbox" :checked="selected.knowledge_ids?.includes(base.id)" @change="selected.knowledge_ids = toggleId(selected.knowledge_ids || [], base.id)" /> {{ base.name }}</label>
        <span v-if="!bases.length" class="sub">这个部门还没有知识库。</span>
      </div>
      <p class="sub">MCP 服务</p>
      <div class="checks">
        <label v-for="server in mcpServers" :key="server.id" class="check"><input type="checkbox" :checked="selected.mcp_ids?.includes(server.id)" @change="selected.mcp_ids = toggleId(selected.mcp_ids || [], server.id)" /> {{ server.name }}</label>
        <span v-if="!mcpServers.length" class="sub">这个部门还没有 MCP 服务。</span>
      </div>
      <p class="sub">技能</p>
      <div class="checks">
        <label v-for="skill in skills" :key="skill.id" class="check"><input type="checkbox" :checked="selected.skill_ids?.includes(skill.id)" @change="selected.skill_ids = toggleId(selected.skill_ids || [], skill.id)" /> {{ skill.name }}</label>
        <span v-if="!skills.length" class="sub">这个部门还没有技能。</span>
      </div>
    </section>
    <template #footer>
      <div class="row">
        <label class="field"><span>在线测试</span><input v-model="runMessage" /></label>
        <label class="check"><input type="checkbox" v-model="testStream" /> 流式输出</label>
        <button class="ghost" type="button" :disabled="running" @click="testFlow">运行测试</button>
      </div>
      <div v-if="liveLogs.length" class="live-log">
        <article v-for="(line, index) in liveLogs" :key="index" class="log-line">
          <strong>{{ line.event }}</strong>
          <span class="sub"> {{ line.tool ? `${line.agent} · ${line.tool}` : (line.agent || line.node) }} · {{ statusText(line.status) }}</span>
          <template v-if="line.event === '工具'">
            <p class="kicker">入参</p>
            <pre class="tool-io">{{ prettyJson(line.input) }}</pre>
            <p class="kicker">输出</p>
            <pre class="tool-io">{{ line.output || "—" }}</pre>
          </template>
          <template v-else>
            <p class="sub" v-if="line.input">入：{{ line.input }}</p>
            <p class="sub" v-if="line.output">出：{{ line.output }}</p>
          </template>
        </article>
      </div>
      <p v-if="runOutput" class="sub">{{ runOutput }}</p>
    </template>
  </Dialog>
  <Dialog wide :open="runOpen" mode="run" :title="runForm?.name || '执行'" @close="runOpen = false">
    <p class="sub">{{ runForm?.release === "published" ? "已发布" : "草稿（操作者需发布后才能执行）" }}</p>
    <label class="field"><span>问题</span><textarea v-model="runForm.message" rows="3" placeholder="这次要完成的问题" /></label>
    <label class="check"><input type="checkbox" v-model="runForm.stream" :disabled="runForm.scheduled" /> 流式输出（默认关闭；定时执行时不可用）</label>
    <label class="check"><input type="checkbox" v-model="runForm.scheduled" /> 定时执行。勾选后按间隔重复这次的问题和节点配置，不立即开工。需已发布。</label>
    <label v-if="runForm?.scheduled" class="field"><span>间隔（分钟）</span><input v-model.number="runForm.everyMinutes" type="number" min="1" step="1" /></label>
    <label v-if="runForm?.scheduled" class="field"><span>定时 token 上限（0 不限）</span><input v-model.number="runForm.tokenBudget" type="number" min="0" step="1" /></label>
    <p v-if="runForm?.scheduled" class="sub">已用 {{ countText(runForm.tokenUsed || 0) }} token<template v-if="runForm.nextAt"> · 下次 {{ runForm.nextAt }}</template></p>
    <section v-for="node in runForm?.nodes || []" :key="node.id" class="node-setup">
      <p class="kicker">{{ agentName(node.agent_id) }}</p>
      <label class="field"><span>提示词</span><textarea v-model="node.prompt" rows="4" placeholder="会按本次任务自动填写，也可以再改" /></label>
      <p class="sub">工具</p>
      <div class="checks">
        <label v-for="tool in tools" :key="tool.id" class="check"><input type="checkbox" :checked="node.tool_names?.includes(tool.name)" @change="node.tool_names = toggleName(node.tool_names || [], tool.name)" /> {{ tool.name }}</label>
      </div>
      <p class="sub">知识库</p>
      <div class="checks">
        <label v-for="base in bases" :key="base.id" class="check"><input type="checkbox" :checked="node.knowledge_ids?.includes(base.id)" @change="node.knowledge_ids = toggleId(node.knowledge_ids || [], base.id)" /> {{ base.name }}</label>
      </div>
      <p class="sub">MCP 服务</p>
      <div class="checks">
        <label v-for="server in mcpServers" :key="server.id" class="check"><input type="checkbox" :checked="node.mcp_ids?.includes(server.id)" @change="node.mcp_ids = toggleId(node.mcp_ids || [], server.id)" /> {{ server.name }}</label>
        <span v-if="!mcpServers.length" class="sub">这个部门还没有 MCP 服务。</span>
      </div>
      <p class="sub">技能</p>
      <div class="checks">
        <label v-for="skill in skills" :key="skill.id" class="check"><input type="checkbox" :checked="node.skill_ids?.includes(skill.id)" @change="node.skill_ids = toggleId(node.skill_ids || [], skill.id)" /> {{ skill.name }}</label>
      </div>
    </section>
    <template #footer>
      <button class="btn" type="button" :disabled="running" @click="startRun">{{ running ? "运行中" : (runForm?.scheduled ? "确认定时" : "确认执行") }}</button>
      <p v-if="runOutput" class="sub">{{ runOutput }}</p>
      <div v-if="liveLogs.length" class="live-log">
        <article v-for="(line, index) in liveLogs" :key="index" class="log-line">
          <strong>{{ line.event }}</strong>
          <span class="sub"> {{ line.tool ? `${line.agent} · ${line.tool}` : (line.agent || line.node) }} · {{ statusText(line.status) }}</span>
          <template v-if="line.event === '工具'">
            <p class="kicker">入参</p>
            <pre class="tool-io">{{ prettyJson(line.input) }}</pre>
            <p class="kicker">输出</p>
            <pre class="tool-io">{{ line.output || "—" }}</pre>
          </template>
          <template v-else>
            <p class="sub" v-if="line.input">入：{{ line.input }}</p>
            <p class="sub" v-if="line.output">出：{{ line.output }}</p>
          </template>
        </article>
      </div>
    </template>
  </Dialog>
  <Dialog :open="confirmOpen" mode="view" :title="pendingAction === 'schedule' ? '确认定时' : '确认执行'" @close="confirmOpen = false">
    <p class="sub">请确认将使用的模型与费用参考，再开工。</p>
    <p class="sub">部门剩余 token {{ countText(estimate?.space_token_remaining || 0) }} / {{ countText(estimate?.space_token_budget || 0) }}</p>
    <p class="sub">费用参考约 {{ moneyText(estimate?.estimated_cost_hint || 0) }}（按每节点约 1千入+1千出 token，单价为元/百万 token）</p>
    <ul class="sub" v-if="estimate?.agents?.length">
      <li v-for="item in estimate.agents" :key="`${item.node_id}-${item.agent_id}`">
        {{ item.agent_name }} · {{ item.model_name }}
        <template v-if="item.version != null"> · v{{ item.version }}</template>
        · 入 {{ moneyText(item.price_in) }}/百万 · 出 {{ moneyText(item.price_out) }}/百万
      </li>
    </ul>
    <template #footer>
      <button class="btn" type="button" :disabled="running" @click="confirmRun">确认</button>
      <button class="ghost" type="button" @click="confirmOpen = false">取消</button>
    </template>
  </Dialog>
</template>
