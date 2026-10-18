<script setup>
import { onMounted, ref } from "vue";
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
</script>

<template>
  <div class="top">
    <div>
      <p class="kicker">08 单次运行</p>
      <h1 v-if="trace">{{ trace.status }}</h1>
    </div>
    <button class="ghost" type="button" @click="back">返回</button>
  </div>
  <p class="error">{{ error }}</p>
  <div v-if="trace" class="grid-2">
    <div class="sheet">
      <p class="sub">{{ trace.input }}</p>
      <div class="md" v-html="renderMarkdown(String(trace.output || '').replace(/^【v\d+】/, ''))"></div>
      <p class="sub" :title="`${trace.cost} 元 · ${trace.latency_ms} 毫秒 · ${trace.tokens} token`">费用 {{ moneyText(trace.cost) }} · {{ durationText(trace.latency_ms) }} · {{ countText(trace.tokens) }} token</p>
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
        <strong>{{ step.kind }} · {{ step.name }}</strong>
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
