<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";
import { countText, durationText, moneyText, prettyJson } from "../format";
import { renderMarkdown } from "../markdown";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const desk = useDesk("/api/traces");
const open = ref(false);
const mode = ref("view");
const form = ref({ steps: [], hops: [] });
const columns = [
  {
    key: "agent_name",
    label: "智能体",
    format: (_value, row) => {
      if (!row.agent_name && !row.agent_key) return "—";
      if (row.agent_kind === "remote") return `${row.agent_name || row.agent_key} · 远程`;
      return row.agent_name || row.agent_key;
    },
  },
  { key: "status", label: "状态" },
  { key: "environment", label: "环境" },
  { key: "input", label: "输入" },
  { key: "cost", label: "费用", format: (value) => moneyText(value) },
  { key: "latency_ms", label: "耗时", format: (value) => durationText(value) },
  { key: "tokens", label: "token", format: (value) => countText(value) },
  { key: "created_at", label: "时间", format: when },
];
function stepTitle(step) {
  if (step.kind !== "remote") return `${step.kind} · ${step.name}`;
  if (step.name === "async-accepted") return "远程 · 第一次返回（已受理，等待回调）";
  if (step.name === "async-complete") return "远程 · 第二次返回（回调完成）";
  if (step.name === "http_async") return "远程 · 发起异步调用";
  if (step.name === "http_sync") return "远程 · 同步调用";
  if (step.name === "a2a") return "远程 · A2A 调用";
  return `远程 · ${step.name}`;
}
function agentLine(row) {
  if (!row?.agent_name && !row?.agent_key) return "";
  const bits = [row.agent_name || row.agent_key];
  if (row.agent_key && row.agent_name) bits.push(row.agent_key);
  if (row.agent_kind === "remote") bits.push(`远程 · ${row.remote_protocol || "remote"}`);
  return bits.join(" · ");
}
async function openWith(row, next) {
  form.value = await api(`/api/traces/${row.id}`);
  form.value.vote = form.value.quality_vote || 1;
  mode.value = next;
  open.value = true;
}
async function save() {
  try {
    await api(`/api/traces/${form.value.id}/feedback`, { method: "POST", body: { vote: Number(form.value.vote) } });
    open.value = false;
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
async function remove(row) {
  if (!window.confirm("删除这条轨迹？")) return;
  try { await api(`/api/traces/${row.id}`, { method: "DELETE" }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条？`)) return;
  try { await api("/api/traces/bulk-delete", { method: "POST", body: { ids: desk.selected.value } }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
function markdown(value) {
  return renderMarkdown(String(value || "").replace(/^【v\d+】/, ""));
}
onMounted(() => desk.refresh());
</script>

<template>
  <Desk kicker="08 轨迹" title="调用记录" sub="轨迹由运行生成。编辑只改评价，不改当时的输入输出。" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :exact-fields="[{ value: 'status', label: '状态' }, { value: 'environment', label: '环境' }]" :error="desk.error.value" :selected="desk.selected.value" :creatable="false" @update:selected="desk.selected.value = $event" @load="desk.refresh()" @view="openWith($event, 'view')" @edit="openWith($event, 'edit')" @remove="remove" @bulk="bulk" />
  <Dialog wide :open="open" :mode="mode" title="轨迹" @close="open = false" @submit="save">
    <p v-if="agentLine(form)" class="sub">
      智能体
      <router-link v-if="form.agent_id" :to="`/agents/${form.agent_id}`">{{ agentLine(form) }}</router-link>
      <template v-else>{{ agentLine(form) }}</template>
      <template v-if="form.workflow_name"> · 工作流 {{ form.workflow_name }}</template>
    </p>
    <p class="sub">{{ form.input }}</p>
    <div class="md" v-html="markdown(form.output)"></div>
    <p v-if="form.agent_kind === 'remote' && form.remote_protocol === 'http_async'" class="sub">
      异步远程会先返回「已受理」，远端算完后再回调写入最终回答；下面步骤里的两次返回对应这两段。
    </p>
    <label class="field"><span>评价</span>
      <select v-model="form.vote"><option :value="1">有用</option><option :value="-1">无用</option></select>
    </label>
    <article class="pipe" v-for="step in form.steps || []" :key="step.idx">
      <strong>{{ stepTitle(step) }}</strong>
      <p class="sub" v-if="step.latency_ms">{{ durationText(step.latency_ms) }}<span v-if="step.tokens"> · {{ countText(step.tokens) }} token</span><span v-if="step.cost"> · {{ moneyText(step.cost) }}</span></p>
      <template v-if="step.tool_args">
        <p class="kicker">入参</p>
        <pre class="tool-io">{{ prettyJson(step.tool_args) }}</pre>
      </template>
      <template v-if="step.output">
        <p class="kicker">输出</p>
        <pre class="tool-io">{{ step.output }}</pre>
      </template>
    </article>
    <template #footer>
      <div class="row">
        <button class="ghost" type="button" @click="open = false">返回</button>
        <router-link :to="`/traces/${form.id}`">打开单页</router-link>
      </div>
    </template>
  </Dialog>
</template>
