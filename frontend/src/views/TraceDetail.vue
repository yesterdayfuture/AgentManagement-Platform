<script setup>
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { countText, durationText, moneyText, prettyJson } from "../format";
import { renderMarkdown } from "../markdown";

const route = useRoute();
const router = useRouter();
const trace = ref(null);
const error = ref("");
onMounted(async () => {
  try { trace.value = await api(`/api/traces/${route.params.id}`); } catch (err) { error.value = err.message; }
});
async function vote(value) {
  trace.value = { ...trace.value, ...(await api(`/api/traces/${route.params.id}/feedback`, { method: "POST", body: { vote: value } })) };
}
function back() {
  if (window.history.state?.back) router.back();
  else router.push("/traces");
}
const agentLine = computed(() => {
  const row = trace.value;
  if (!row?.agent_name && !row?.agent_key) return "";
  const bits = [row.agent_name || row.agent_key];
  if (row.agent_key && row.agent_name) bits.push(row.agent_key);
  if (row.agent_kind === "remote") bits.push(`远程 · ${row.remote_protocol || "remote"}`);
  else if (row.agent_kind) bits.push("本地");
  return bits.join(" · ");
});
const isAsyncRemote = computed(() => {
  const row = trace.value;
  return row?.agent_kind === "remote" && row?.remote_protocol === "http_async";
});
function stepTitle(step) {
  if (step.kind !== "remote") return `${step.kind} · ${step.name}`;
  if (step.name === "async-accepted") return "远程 · 第一次返回（已受理，等待回调）";
  if (step.name === "async-complete") return "远程 · 第二次返回（回调完成）";
  if (step.name === "http_async") return "远程 · 发起异步调用";
  if (step.name === "http_sync") return "远程 · 同步调用";
  if (step.name === "a2a") return "远程 · A2A 调用";
  return `远程 · ${step.name}`;
}
</script>

<template>
  <div class="top">
    <div>
      <p class="kicker">08 单次运行</p>
      <h1 v-if="trace">{{ trace.status }}</h1>
      <p v-if="trace && agentLine" class="sub">
        智能体
        <router-link v-if="trace.agent_id" :to="`/agents/${trace.agent_id}`">{{ agentLine }}</router-link>
        <template v-else>{{ agentLine }}</template>
        <template v-if="trace.workflow_name"> · 工作流 {{ trace.workflow_name }}</template>
        · {{ trace.environment }}
      </p>
    </div>
    <button class="ghost" type="button" @click="back">返回</button>
  </div>
  <p class="error">{{ error }}</p>
  <div v-if="trace" class="grid-2">
    <div class="sheet">
      <p class="sub">{{ trace.input }}</p>
      <div class="md" v-html="renderMarkdown(String(trace.output || '').replace(/^【v\d+】/, ''))"></div>
      <p class="sub" :title="`${trace.cost} 元 · ${trace.latency_ms} 毫秒 · ${trace.tokens} token`">费用 {{ moneyText(trace.cost) }} · {{ durationText(trace.latency_ms) }} · {{ countText(trace.tokens) }} token</p>
      <p v-if="isAsyncRemote" class="sub">
        异步远程：平台先收到「已受理」并记为 waiting；远端算完后向
        <code>/api/agents/{{ trace.agent_id }}/remote-complete</code>
        回调，轨迹才写成最终回答。右侧步骤里的两次「返回」对应这两段。
      </p>
      <div class="row">
        <button class="ghost" @click="vote(1)">有用</button>
        <button class="ghost" @click="vote(-1)">无用</button>
      </div>
      <h2 style="margin-top: 18px">委托链</h2>
      <div class="pipe" v-for="hop in trace.hops" :key="hop.seq">{{ hop.credential }} · {{ hop.actor }} → {{ hop.target }}</div>
    </div>
    <div class="sheet">
      <h2>步骤</h2>
      <article class="pipe" v-for="step in trace.steps" :key="step.idx">
        <strong>{{ stepTitle(step) }}</strong>
        <div class="sub" v-if="step.model">模型 {{ step.model }} <span v-if="step.fallback">· 已回退规则</span></div>
        <p class="sub" v-if="step.latency_ms || step.tokens || step.cost">{{ durationText(step.latency_ms) }}<span v-if="step.tokens"> · {{ countText(step.tokens) }} token</span><span v-if="step.cost"> · {{ moneyText(step.cost) }}</span></p>
        <template v-if="step.tool_args">
          <p class="kicker">入参</p>
          <pre class="tool-io">{{ prettyJson(step.tool_args) }}</pre>
        </template>
        <template v-if="step.output">
          <p class="kicker">输出</p>
          <pre class="tool-io">{{ step.output }}</pre>
        </template>
      </article>
    </div>
  </div>
</template>
