<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import DistributionChart from "../components/DistributionChart.vue";
import { api } from "../api";
import { axisText, countText, formatCount, formatDuration, formatMoney, moneyText } from "../format";

const summary = ref(null);
const system = ref(null);
const traces = ref([]);
const error = ref("");
const loading = ref(false);
let loadId = 0;
const range = reactive({ time_from: "", time_to: "" });

const profile = ref(null);
const platform = computed(() => Boolean(profile.value?.platform_admin));
const spaces = computed(() => profile.value?.spaces || []);
const departmentId = ref("");
const chartKind = ref("bar");
const metric = ref("traffic");
const metrics = [
  { value: "traffic", label: "调用" },
  { value: "cost", label: "费用" },
  { value: "tokens", label: "token" },
];
const scopeLabel = computed(() => {
  if (!platform.value) return "当前部门";
  const found = spaces.value.find((item) => String(item.id) === String(departmentId.value));
  return found?.name || "全部部门";
});
const maxModel = computed(() => Math.max(0.000001, ...(summary.value?.by_model || []).map((item) => item.cost)));
const modelCalls = computed(() => (summary.value?.by_model || []).reduce((sum, item) => sum + (item.calls || item.steps || 0), 0));
const modelSecretMetric = ref("calls");
const modelSecretMetrics = [
  { value: "calls", label: "调用" },
  { value: "tokens", label: "token" },
  { value: "cost", label: "费用" },
];
function trendChart(rows, metricName) {
  const width = 640;
  const height = 196;
  const pad = { left: 44, right: 12, top: 16, bottom: 28 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const max = Math.max(1, ...rows.map((row) => Number(row[metricName]) || 0));
  const count = Math.max(rows.length, 1);
  const points = rows.map((row, index) => {
    const value = Number(row[metricName]) || 0;
    const slot = innerW / count;
    const barW = Math.max(1, Math.min(28, slot * .65));
    const x = pad.left + slot * index + (slot - barW) / 2;
    const y = pad.top + innerH - (value / max) * innerH;
    const lineX = pad.left + (count === 1 ? innerW / 2 : (index / (rows.length - 1)) * innerW);
    return { x, y, lineX, barW, value, day: row.day, height: Math.max(value ? 2 : 0, (value / max) * innerH) };
  });
  return {
    width,
    height,
    pad,
    innerH,
    max,
    points,
    line: points.map((point) => `${point.lineX},${point.y}`).join(" "),
    baseline: pad.top + innerH,
    ticks: [0, .5, 1].map((ratio) => ({ y: pad.top + innerH * (1 - ratio), label: axisText(max * ratio, metricName) })),
  };
}
const chart = computed(() => trendChart(summary.value?.by_day || [], metric.value));
const modelSecretChart = computed(() => trendChart(summary.value?.model_secrets?.by_day || [], modelSecretMetric.value));
const mcpSecretChart = computed(() => trendChart(summary.value?.mcp_secrets?.by_day || [], "calls"));
const maxModelSecretDept = computed(() => Math.max(1, ...(summary.value?.model_secrets?.by_department || []).map((item) => item.calls)));
const maxMcpSecretDept = computed(() => Math.max(1, ...(summary.value?.mcp_secrets?.by_department || []).map((item) => item.calls)));
const maxUser = computed(() => Math.max(1, ...(summary.value?.by_user || []).map((item) => item.traffic)));
const skillbox = computed(() => system.value?.skillbox || null);
const skillboxChecked = ref("");
let skillTimer = 0;
const skillboxState = computed(() => ({ running: "运行中", exited: "已退出", created: "已创建", dead: "已停止", paused: "已暂停", missing: "未创建", unavailable: "不可用" })[skillbox.value?.state] || skillbox.value?.state || "不可用");
const skillJobs = computed(() => skillbox.value?.jobs || []);
function jobSpan(job) {
  const start = Number(job.started_at || 0);
  const end = job.state === "running" ? Date.now() / 1000 : Number(job.ended_at || start);
  const seconds = Math.max(0, Math.round(end - start));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
function jobState(job) {
  return { running: "正在执行", finished: "刚刚完成", failed: "刚刚失败" }[job.state] || "正在执行";
}
function jobLabel(job) {
  return job.skill_name || (job.skill_key ? job.skill_key : `技能 ${job.skill_id}`);
}
const resourceItems = computed(() => [
  { label: "工具", value: summary.value?.resources?.tools || 0, to: "/tools" },
  { label: "记忆", value: summary.value?.resources?.memories || 0, to: "/memories" },
  { label: "知识库", value: summary.value?.resources?.knowledge_bases || 0, to: "/knowledge" },
  { label: "文档", value: summary.value?.resources?.documents || 0, to: "/knowledge" },
]);
const maxResource = computed(() => Math.max(1, ...resourceItems.value.map(item => item.value)));
const maxDepartment = computed(() => Math.max(1, ...(summary.value?.by_department || []).map(item => item.traffic)));
const statusLabels = { completed: "已完成", failed: "失败", running: "运行中", pending: "待处理", denied: "已拒绝", cancelled: "已取消", blocked: "已阻止", paused: "已暂停" };
const statusItems = computed(() => Object.entries(summary.value?.by_status || {}).map(([key, value]) => ({ label: statusLabels[key] || key, value, color: ({ completed: "#159a9c", failed: "#cc4054", running: "#2864e8", denied: "#d39425" })[key] })));
const tokenItems = computed(() => [{ label: "输入 token", value: summary.value?.tokens_in || 0 }, { label: "输出 token", value: summary.value?.tokens_out || 0 }]);
const envTotal = computed(() => Object.values(summary.value?.by_environment || {}).reduce((sum, value) => sum + value, 0) || 1);

function params() {
  const search = new URLSearchParams();
  if (range.time_from) search.set("time_from", range.time_from);
  if (range.time_to) search.set("time_to", range.time_to);
  return search;
}

function resetRange() {
  range.time_from = "";
  range.time_to = "";
  departmentId.value = "";
  load();
}
function pickDepartment(id) {
  departmentId.value = id ? String(id) : "";
  load();
}

async function load() {
  const requestId = ++loadId;
  loading.value = true;
  error.value = "";
  const search = params();
  const summaryQuery = new URLSearchParams(search);
  const traceQuery = new URLSearchParams(search);
  traceQuery.set("page", "1");
  traceQuery.set("page_size", "8");
  let me = profile.value;
  try {
    me = await api("/api/me");
    if (requestId !== loadId) return;
    profile.value = me;
  } catch (err) {
    if (requestId === loadId) { error.value = err.message; loading.value = false; }
    return;
  }
  const seesAll = Boolean(me?.platform_admin);
  const chosen = seesAll ? spaces.value.find((item) => String(item.id) === String(departmentId.value)) : null;
  if (seesAll && departmentId.value && !chosen) departmentId.value = "";
  if (seesAll && departmentId.value) {
    summaryQuery.set("space_id", departmentId.value);
    traceQuery.set("space_id", departmentId.value);
  } else if (seesAll) {
    summaryQuery.set("scope", "all");
  } else {
    summaryQuery.set("scope", "space");
  }
  try {
    const [board, info, page] = await Promise.all([
      api(`/api/observability/summary?${summaryQuery}`),
      api("/api/system"),
      api(`/api/traces?${traceQuery}`),
    ]);
    if (requestId !== loadId) return;
    summary.value = board;
    system.value = info;
    traces.value = page.items || [];
    if (info?.skillbox) skillboxChecked.value = clockText(new Date());
  } catch (err) {
    if (requestId === loadId) error.value = err.message;
  } finally {
    if (requestId === loadId) loading.value = false;
  }
}
function clockText(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
async function refreshSkillbox() {
  if (document.visibilityState === "hidden") return;
  try {
    const info = await api("/api/system");
    system.value = system.value ? { ...system.value, skillbox: info.skillbox } : info;
    skillboxChecked.value = clockText(new Date());
  } catch {
    skillboxChecked.value = "";
  }
}
function watchSkillbox() {
  window.clearInterval(skillTimer);
  skillTimer = window.setInterval(refreshSkillbox, 1000);
}
function onVisible() {
  if (document.visibilityState === "visible") refreshSkillbox();
}
onMounted(() => {
  load();
  watchSkillbox();
  document.addEventListener("visibilitychange", onVisible);
});
onUnmounted(() => {
  window.clearInterval(skillTimer);
  document.removeEventListener("visibilitychange", onVisible);
});
</script>

<template>
  <section class="overview-page">
  <header class="overview-header">
    <div><p class="kicker">运行观测 / OVERVIEW</p><h1>运行总览</h1><p class="sub">集中查看部门调用、资源分布与运行质量。</p></div>
    <label v-if="platform" class="field department-filter"><span>统计部门</span>
      <select aria-label="统计部门" v-model="departmentId" @change="load">
        <option value="">全部部门</option>
        <option v-for="item in spaces" :key="item.id" :value="String(item.id)">{{ item.name }}</option>
      </select>
    </label>
    <span v-else class="role-badge">当前部门</span>
  </header>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <div class="row filters">
    <label>开始时间<input v-model="range.time_from" type="datetime-local" /></label>
    <label>结束时间<input v-model="range.time_to" type="datetime-local" /></label>
    <button class="ghost" type="button" @click="load" :disabled="loading">按时间过滤</button>
    <button class="ghost" type="button" @click="resetRange" :disabled="loading">重置</button>
    <span class="overview-scope" role="status">{{ loading ? "正在更新数据…" : `统计范围：${scopeLabel}` }}</span>
  </div>
  <div class="overview-results" :aria-busy="loading" :class="{ refreshing: loading }">
  <section class="sheet skillbox-panel" v-if="skillbox" aria-label="技能容器">
    <div class="top">
      <div>
        <h2>技能容器</h2>
        <p class="sub">每 1 秒检查一次。{{ skillboxChecked ? `最近检查 ${skillboxChecked}` : "正在检查…" }}。进程退出时容器会销毁。</p>
      </div>
      <span class="role-badge" :class="{ on: skillbox.state === 'running' }" aria-live="polite"><i></i>{{ skillboxState }}</span>
    </div>
    <div class="skillbox-grid">
      <div><span>容器</span><b>{{ skillbox.name }}</b></div>
      <div><span>镜像</span><b>{{ skillbox.image || "—" }}</b></div>
      <div><span>启动时间</span><b>{{ skillbox.started_at ? skillbox.started_at.replace("T", " ").replace("Z", " UTC").slice(0, 19) : "—" }}</b></div>
      <div><span>网络</span><b>{{ skillbox.network || "—" }}</b></div>
      <div><span>内存上限</span><b>{{ skillbox.memory || "—" }}</b></div>
      <div><span>CPU</span><b>{{ skillbox.cpus || "—" }}</b></div>
      <div><span>根文件系统</span><b>{{ skillbox.read_only ? "只读" : "可写" }}</b></div>
      <div><span>Python</span><b>{{ skillbox.python || "—" }}</b></div>
      <div><span>Node</span><b>{{ skillbox.node || "—" }}</b></div>
      <div><span>Java</span><b>{{ skillbox.java || "—" }}</b></div>
    </div>
    <div class="skillbox-jobs" aria-live="polite">
      <h3>脚本执行</h3>
      <p v-if="!skillJobs.length">当前没有脚本在执行。</p>
      <ul v-else>
        <li v-for="job in skillJobs" :key="job.id" :data-state="job.state || 'running'">
          <em>{{ jobState(job) }}</em>
          <b>{{ jobLabel(job) }}</b>
          <code>{{ job.script }}</code>
          <span>{{ job.state === "running" ? "已运行" : "用时" }} {{ jobSpan(job) }}</span>
        </li>
      </ul>
    </div>
    <p v-if="skillbox.error" class="error" role="alert">{{ skillbox.error }}</p>
  </section>
  <div class="stats overview-metrics" v-if="summary">
    <router-link class="stat" to="/traces"><b>{{ formatCount(summary.traffic).value }}</b><span>调用总量{{ formatCount(summary.traffic).unit }}</span></router-link>
    <router-link class="stat" to="/models"><b>{{ formatMoney(summary.cost).value }}</b><span>累计费用 {{ formatMoney(summary.cost).unit }}</span></router-link>
    <router-link class="stat" to="/models"><b>{{ formatCount(summary.tokens).value }}</b><span>总 token{{ formatCount(summary.tokens).unit ? ` ${formatCount(summary.tokens).unit}` : "" }}</span></router-link>
    <router-link class="stat" to="/traces" :title="`${summary.latency_ms} 毫秒`"><b>{{ formatDuration(summary.latency_ms).value }}</b><span>平均耗时 {{ formatDuration(summary.latency_ms).unit }}</span></router-link>
    <router-link class="stat" to="/evals"><b>{{ Math.round(summary.quality * 100) }}%</b><span>完成质量</span></router-link>
  </div>
  <div class="overview-chart-grid" v-if="summary">
    <section class="sheet overview-visual-card">
      <h2>部门调用分布</h2><p class="sub">{{ scopeLabel }} · 当前时间范围</p>
      <div class="bars">
        <div v-for="item in summary.by_department" :key="item.space_id" class="bar-row">
          <span :title="item.department">{{ item.department }}</span>
          <div class="bar"><i :style="{ width: `${item.traffic / maxDepartment * 100}%` }"></i></div><b class="mono" :title="String(item.traffic)">{{ countText(item.traffic) }}</b>
        </div>
      </div><p v-if="!summary.by_department?.length" class="sub">当前范围暂无部门数据</p>
    </section>
    <section class="sheet overview-visual-card"><h2>Token 消耗构成</h2><p class="sub">输入与输出占比 · 当前时间范围</p><DistributionChart label="Token 消耗构成" :items="tokenItems" unit="token" /></section>
    <section class="sheet overview-visual-card"><h2>资源规模</h2><p class="sub">当前登记数量 · 不受时间筛选影响</p>
      <div class="bars resource-bars"><router-link v-for="item in resourceItems" :key="item.label" :to="item.to" class="bar-row"><span>{{ item.label }}</span><div class="bar"><i :style="{ width: `${item.value / maxResource * 100}%` }"></i></div><b class="mono" :title="String(item.value)">{{ countText(item.value) }}</b></router-link></div>
    </section>
  </div>
  <p class="sub" v-if="summary?.resources">工具、记忆、知识库和文档是{{ scopeLabel }}里的登记数量，不随上方的调用时间变化。全局内置工具只计一次。总 token 是这段时间里输入和输出 token 之和。</p>
  <div class="stat-row" v-if="summary">
    <div class="sheet">
      <h2>{{ platform ? "各部门统计" : "本部门统计" }}</h2>
      <div class="table-scroll" tabindex="0" role="region" aria-label="部门统计">
        <table class="compact-table department-table">
          <thead><tr><th>部门</th><th>调用</th><th>费用</th><th>token</th><th>智能体</th><th>工作流</th><th>模型</th><th>工具</th><th>知识库</th><th>记忆</th></tr></thead>
          <tbody>
            <tr v-for="item in summary.by_department" :key="item.space_id" :class="{ 'is-selected': platform && String(departmentId) === String(item.space_id) }">
              <td>
                <button v-if="platform" type="button" class="dept-name" :title="item.department" :aria-pressed="String(departmentId) === String(item.space_id)" @click="pickDepartment(departmentId === String(item.space_id) ? '' : item.space_id)">{{ item.department }}</button>
                <template v-else>{{ item.department }}</template>
              </td>
              <td class="mono" :title="String(item.traffic)">{{ countText(item.traffic) }}</td>
              <td class="mono" :title="String(item.cost)">{{ moneyText(item.cost) }}</td>
              <td class="mono" :title="String(item.tokens || 0)">{{ countText(item.tokens || 0) }}</td>
              <td class="mono">{{ item.agents || 0 }}</td>
              <td class="mono">{{ item.workflows || 0 }}</td>
              <td class="mono">{{ item.models || 0 }}</td>
              <td class="mono">{{ item.tools || 0 }}</td>
              <td class="mono">{{ item.knowledge_bases || 0 }}</td>
              <td class="mono">{{ item.memories || 0 }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <h2 style="margin-top: 22px">模型费用</h2>
      <div class="bars">
        <div class="bar-row" v-for="item in summary.by_model" :key="item.model">
          <span :title="item.model">{{ item.model }}</span>
          <div class="bar"><i :style="{ width: `${(item.cost / maxModel) * 100}%` }"></i></div>
          <b class="mono" :title="String(item.cost)">{{ moneyText(item.cost) }}</b>
        </div>
      </div>
    </div>
    <div>
      <div class="sheet">
        <div class="top">
          <h2>随时间变化</h2>
          <div class="row">
            <button v-for="item in metrics" :key="item.value" class="ghost" type="button" :aria-pressed="metric === item.value" @click="metric = item.value">{{ item.label }}</button>
            <button class="ghost" type="button" :aria-pressed="chartKind === 'bar'" @click="chartKind = 'bar'">柱状图</button>
            <button class="ghost" type="button" :aria-pressed="chartKind === 'line'" @click="chartKind = 'line'">折线图</button>
          </div>
        </div>
        <svg v-if="chart.points.length" class="trend-chart" :viewBox="`0 0 ${chart.width} ${chart.height}`" role="img" :aria-label="`${scopeLabel}的${metrics.find((item) => item.value === metric).label}趋势`">
          <g v-for="tick in chart.ticks" :key="tick.y"><line class="chart-gridline" :x1="chart.pad.left" :x2="chart.width - chart.pad.right" :y1="tick.y" :y2="tick.y" /><text :x="chart.pad.left - 6" :y="tick.y + 3" text-anchor="end">{{ tick.label }}</text></g>
          <line :x1="chart.pad.left" :x2="chart.width - chart.pad.right" :y1="chart.baseline" :y2="chart.baseline" />
          <template v-if="chartKind === 'bar'">
            <rect v-for="point in chart.points" :key="point.day" :x="point.x" :y="point.y" :width="point.barW" :height="point.height" rx="3">
              <title>{{ point.day }} {{ axisText(point.value, metric) }}</title>
            </rect>
          </template>
          <template v-else>
            <polyline :points="chart.line" />
            <circle v-for="point in chart.points" :key="point.day" :cx="point.lineX" :cy="point.y" r="3.5">
              <title>{{ point.day }} {{ axisText(point.value, metric) }}</title>
            </circle>
          </template>
          <text v-for="point in chart.points.filter((_, index) => index % Math.max(1, Math.ceil(chart.points.length / 6)) === 0 || index === chart.points.length - 1)" :key="`${point.day}-label`" :x="chartKind === 'bar' ? point.x + point.barW / 2 : point.lineX" :y="chart.height - 8" text-anchor="middle">{{ point.day }}</text>
        </svg>
        <p v-else class="sub">这段时间没有轨迹。</p>
      </div>
      <div class="sheet">
        <h2>状态与环境</h2>
        <DistributionChart label="调用状态分布" :items="statusItems" />
        <h3 class="chart-caption">运行环境</h3>
        <div class="split">
          <i v-for="(count, name) in summary.by_environment" :key="name" :style="{ width: `${(count / envTotal) * 100}%` }" :title="`${name} ${count}`"></i>
        </div>
        <div class="environment-legend"><span v-for="(count, name) in summary.by_environment" :key="name"><i></i>{{ name }} · {{ count }}（{{ (count / envTotal * 100).toFixed(1) }}%）</span><span v-if="!Object.keys(summary.by_environment || {}).length">暂无环境数据</span></div>
        <p class="sub">令牌 {{ countText(summary.tokens) }} · 好评 {{ summary.positive_feedback_rate ?? "—" }}</p>
        <div class="system-controls" v-if="system">
          <span v-for="(on, key) in system.controls" :key="key"><i :class="{ on }"></i>{{ key }} · {{ on ? "生效" : "关闭" }}</span>
        </div>
      </div>
    </div>
  </div>
  <div class="stats" v-if="summary">
    <router-link class="stat" to="/models"><b>{{ formatCount(modelCalls).value }}</b><span>模型调用{{ formatCount(modelCalls).unit }}</span></router-link>
    <router-link class="stat" to="/models"><b>{{ formatMoney(summary.quota_used_total).value }}</b><span>已用额度 {{ formatMoney(summary.quota_used_total).unit }}</span></router-link>
    <router-link class="stat" to="/models"><b>{{ formatMoney(summary.quota_remaining_total).value }}</b><span>剩余额度 {{ formatMoney(summary.quota_remaining_total).unit }}</span></router-link>
  </div>
  <p class="sub" v-if="summary">额度按调用费用累计。未设置上限的模型不计入剩余额度。调用次数和热门问题随上方时间范围变化。</p>
  <div class="sheet" v-if="summary">
    <h2>模型调用与额度</h2>
    <div class="table-scroll" tabindex="0" role="region" aria-label="模型额度">
      <table class="compact-table quota-table">
        <thead><tr><th>模型</th><th>调用</th><th>输入 token</th><th>输出 token</th><th>token</th><th>已用</th><th>上限</th><th>剩余</th></tr></thead>
        <tbody>
          <tr v-if="!summary.by_model.length"><td colspan="8" class="empty-cell">这段时间没有模型调用。</td></tr>
          <tr v-for="item in summary.by_model" :key="item.model">
            <td :title="item.model">{{ item.model }}</td>
            <td class="mono" :title="String(item.calls || item.steps)">{{ countText(item.calls || item.steps) }}</td>
            <td class="mono" :title="String(item.tokens_in || 0)">{{ countText(item.tokens_in || 0) }}</td>
            <td class="mono" :title="String(item.tokens_out || 0)">{{ countText(item.tokens_out || 0) }}</td>
            <td class="mono" :title="String(item.tokens || 0)">{{ countText(item.tokens || 0) }}</td>
            <td class="mono" :title="String(item.quota_used)">{{ moneyText(item.quota_used) }}</td>
            <td class="mono">{{ item.quota_limit == null ? "不限" : moneyText(item.quota_limit) }}</td>
            <td class="mono">{{ item.quota_remaining == null ? "不限" : moneyText(item.quota_remaining) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
  <div class="stats" v-if="summary">
    <router-link class="stat" to="/models"><b>{{ formatCount(summary.model_secrets?.calls || 0).value }}</b><span>模型密钥调用{{ formatCount(summary.model_secrets?.calls || 0).unit }}</span></router-link>
    <router-link class="stat" to="/models"><b>{{ formatMoney(summary.model_secrets?.cost || 0).value }}</b><span>模型密钥费用 {{ formatMoney(summary.model_secrets?.cost || 0).unit }}</span></router-link>
    <router-link class="stat" to="/models"><b>{{ formatCount(summary.model_secrets?.tokens || 0).value }}</b><span>模型密钥 token{{ formatCount(summary.model_secrets?.tokens || 0).unit ? ` ${formatCount(summary.model_secrets?.tokens || 0).unit}` : "" }}</span></router-link>
    <router-link class="stat" to="/tools?tab=mcp"><b>{{ formatCount(summary.mcp_secrets?.calls || 0).value }}</b><span>MCP 密钥调用{{ formatCount(summary.mcp_secrets?.calls || 0).unit }}</span></router-link>
  </div>
  <p class="sub" v-if="summary">密钥统计跟上方时间范围和部门范围一致。带密钥编号的步骤按当时使用的密钥计算；更早的模型调用按该模型现在绑定的密钥归类。规则模型没有密钥，不计入。MCP 旧记录只在该版本只挂了一个带密钥的服务时归入。</p>
  <div class="stat-row" v-if="summary">
    <div class="sheet">
      <div class="top">
        <h2>模型密钥随时间</h2>
        <div class="row">
          <button v-for="item in modelSecretMetrics" :key="item.value" class="ghost" type="button" :aria-pressed="modelSecretMetric === item.value" @click="modelSecretMetric = item.value">{{ item.label }}</button>
        </div>
      </div>
      <svg v-if="modelSecretChart.points.length" class="trend-chart" :viewBox="`0 0 ${modelSecretChart.width} ${modelSecretChart.height}`" role="img" :aria-label="`${scopeLabel}的模型密钥${modelSecretMetrics.find((item) => item.value === modelSecretMetric).label}趋势`">
        <g v-for="tick in modelSecretChart.ticks" :key="tick.y"><line class="chart-gridline" :x1="modelSecretChart.pad.left" :x2="modelSecretChart.width - modelSecretChart.pad.right" :y1="tick.y" :y2="tick.y" /><text :x="modelSecretChart.pad.left - 6" :y="tick.y + 3" text-anchor="end">{{ tick.label }}</text></g>
        <line :x1="modelSecretChart.pad.left" :x2="modelSecretChart.width - modelSecretChart.pad.right" :y1="modelSecretChart.baseline" :y2="modelSecretChart.baseline" />
        <rect v-for="point in modelSecretChart.points" :key="point.day" :x="point.x" :y="point.y" :width="point.barW" :height="point.height" rx="3"><title>{{ point.day }} {{ axisText(point.value, modelSecretMetric) }}</title></rect>
        <text v-for="point in modelSecretChart.points.filter((_, index) => index % Math.max(1, Math.ceil(modelSecretChart.points.length / 6)) === 0 || index === modelSecretChart.points.length - 1)" :key="`${point.day}-label`" :x="point.x + point.barW / 2" :y="modelSecretChart.height - 8" text-anchor="middle">{{ point.day }}</text>
      </svg>
      <p v-else class="sub">这段时间没有使用模型密钥的调用。</p>
      <h2 style="margin-top: 22px">模型密钥按部门</h2>
      <div class="bars">
        <div class="bar-row" v-for="item in summary.model_secrets?.by_department || []" :key="item.space_id">
          <span :title="item.department">{{ item.department }}</span>
          <div class="bar"><i :style="{ width: `${item.calls / maxModelSecretDept * 100}%` }"></i></div>
          <b class="mono" :title="String(item.calls)">{{ countText(item.calls) }}</b>
        </div>
      </div>
      <p v-if="!(summary.model_secrets?.by_department || []).length" class="sub">这段时间没有使用模型密钥的调用。</p>
    </div>
    <div class="sheet">
      <h2>MCP 密钥随时间</h2>
      <svg v-if="mcpSecretChart.points.length" class="trend-chart" :viewBox="`0 0 ${mcpSecretChart.width} ${mcpSecretChart.height}`" role="img" :aria-label="`${scopeLabel}的 MCP 密钥调用趋势`">
        <g v-for="tick in mcpSecretChart.ticks" :key="`mcp-${tick.y}`"><line class="chart-gridline" :x1="mcpSecretChart.pad.left" :x2="mcpSecretChart.width - mcpSecretChart.pad.right" :y1="tick.y" :y2="tick.y" /><text :x="mcpSecretChart.pad.left - 6" :y="tick.y + 3" text-anchor="end">{{ tick.label }}</text></g>
        <line :x1="mcpSecretChart.pad.left" :x2="mcpSecretChart.width - mcpSecretChart.pad.right" :y1="mcpSecretChart.baseline" :y2="mcpSecretChart.baseline" />
        <rect v-for="point in mcpSecretChart.points" :key="point.day" :x="point.x" :y="point.y" :width="point.barW" :height="point.height" rx="3"><title>{{ point.day }} {{ countText(point.value) }}</title></rect>
        <text v-for="point in mcpSecretChart.points.filter((_, index) => index % Math.max(1, Math.ceil(mcpSecretChart.points.length / 6)) === 0 || index === mcpSecretChart.points.length - 1)" :key="`${point.day}-mcp`" :x="point.x + point.barW / 2" :y="mcpSecretChart.height - 8" text-anchor="middle">{{ point.day }}</text>
      </svg>
      <p v-else class="sub">这段时间没有使用 MCP 密钥的调用。</p>
      <h2 style="margin-top: 22px">MCP 密钥按部门</h2>
      <div class="bars">
        <div class="bar-row" v-for="item in summary.mcp_secrets?.by_department || []" :key="item.space_id">
          <span :title="item.department">{{ item.department }}</span>
          <div class="bar"><i :style="{ width: `${item.calls / maxMcpSecretDept * 100}%` }"></i></div>
          <b class="mono" :title="String(item.calls)">{{ countText(item.calls) }}</b>
        </div>
      </div>
      <p v-if="!(summary.mcp_secrets?.by_department || []).length" class="sub">这段时间没有使用 MCP 密钥的调用。</p>
    </div>
  </div>
  <div class="sheet" v-if="summary">
    <h2>各密钥明细</h2>
    <div class="table-scroll" tabindex="0" role="region" aria-label="模型密钥">
      <table class="compact-table">
        <thead><tr><th>模型密钥</th><th>存放部门</th><th>调用</th><th>token</th><th>花费</th><th>调用部门</th></tr></thead>
        <tbody>
          <tr v-if="!(summary.model_secrets?.items || []).length"><td colspan="6" class="empty-cell">这段时间没有使用模型密钥的调用。</td></tr>
          <tr v-for="item in summary.model_secrets?.items || []" :key="`model-${item.secret_id}`">
            <td><router-link :to="`/models?secret_id=${item.secret_id}`" :title="item.name">{{ item.name }}</router-link></td>
            <td>{{ item.department || "—" }}</td>
            <td class="mono" :title="String(item.calls)">{{ countText(item.calls) }}</td>
            <td class="mono" :title="String(item.tokens)">{{ countText(item.tokens) }}</td>
            <td class="mono" :title="String(item.cost)">{{ moneyText(item.cost) }}</td>
            <td :title="item.by_department.map((dept) => `${dept.department} ${dept.calls}`).join('，')">{{ item.by_department.map((dept) => `${dept.department} ${countText(dept.calls)}`).join(" · ") }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="table-scroll" tabindex="0" role="region" aria-label="MCP 密钥" style="margin-top: 18px">
      <table class="compact-table">
        <thead><tr><th>MCP 密钥</th><th>存放部门</th><th>调用</th><th>调用部门</th></tr></thead>
        <tbody>
          <tr v-if="!(summary.mcp_secrets?.items || []).length"><td colspan="4" class="empty-cell">这段时间没有使用 MCP 密钥的调用。</td></tr>
          <tr v-for="item in summary.mcp_secrets?.items || []" :key="`mcp-${item.secret_id}`">
            <td><router-link :to="`/tools?tab=mcp&secret_id=${item.secret_id}`" :title="item.name">{{ item.name }}</router-link></td>
            <td>{{ item.department || "—" }}</td>
            <td class="mono" :title="String(item.calls)">{{ countText(item.calls) }}</td>
            <td :title="item.by_department.map((dept) => `${dept.department} ${dept.calls}`).join('，')">{{ item.by_department.map((dept) => `${dept.department} ${countText(dept.calls)}`).join(" · ") }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
  <div class="stat-row" v-if="summary">
    <div class="sheet">
      <h2>部门与用户</h2>
      <div class="bars">
        <div class="bar-row" v-for="item in summary.by_user" :key="`${item.user_id}-${item.department}`">
          <span :title="`${item.department} · ${item.username}`">{{ item.department }} · {{ item.username }}</span>
          <div class="bar"><i :style="{ width: `${(item.traffic / maxUser) * 100}%` }"></i></div>
          <b class="mono" :title="String(item.traffic)">{{ countText(item.traffic) }}</b>
        </div>
      </div>
      <p v-if="!summary.by_user.length" class="sub">这段时间没有调用。</p>
    </div>
    <div class="sheet">
      <h2>热门问题</h2>
      <div class="bars">
        <div class="bar-row" v-for="item in summary.hot_questions" :key="item.question">
          <span :title="item.question">{{ item.question }}</span>
          <b class="mono" :title="String(item.count)">{{ countText(item.count) }}</b>
        </div>
      </div>
      <p v-if="!summary.hot_questions.length" class="sub">这段时间没有问题。</p>
    </div>
  </div>
  <div class="sheet" v-if="summary">
    <h2>最近轨迹</h2>
    <div class="table-scroll" tabindex="0" role="region" aria-label="最近轨迹列表">
      <table class="compact-table">
      <thead><tr><th>状态</th><th>环境</th><th>输入</th><th>费用</th></tr></thead>
      <tbody>
        <tr v-if="!traces.length"><td colspan="4" class="empty-cell">当前时间范围内暂无调用轨迹。</td></tr>
        <tr v-for="item in traces" :key="item.id">
          <td><router-link :to="`/traces/${item.id}`">{{ item.status }}</router-link></td>
          <td :title="item.environment">{{ item.environment }}</td>
          <td :title="item.input">{{ item.input }}</td>
          <td class="mono" :title="String(item.cost)">{{ moneyText(item.cost) }}</td>
        </tr>
      </tbody>
    </table>
      </div>
  </div>
  </div>
  </section>
</template>
