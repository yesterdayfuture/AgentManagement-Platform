<script setup>
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import Dialog from "../components/Dialog.vue";
import { api, stream } from "../api";
import { countText, moneyText, prettyJson, durationText } from "../format";
import { applyGuide, guideText } from "../toolGuide";

const route = useRoute();
const agent = ref(null);
const tools = ref([]);
const servers = ref([]);
const mcpCatalog = ref({});
const mcpIds = ref([]);
const prompt = ref("");
const selected = ref([]);
const remoteProtocol = ref("http_sync");
const remoteEndpoint = ref("");
const remoteToken = ref("");
const remoteTimeout = ref(30000);
const remoteHealthUrl = ref("");
const remoteHealthMethod = ref("GET");
const message = ref("计算 19*7");
const environment = ref("test");
const useStream = ref(false);
const thread = ref([]);
const resumeTraceId = ref("");
const running = ref(false);
const error = ref("");
const notice = ref("");
const gray = ref(0);
const viewed = ref(null);
const latest = computed(() => agent.value?.versions?.at(-1) || null);
const isRemote = computed(() => agent.value?.kind === "remote");

async function load() {
  agent.value = await api(`/api/agents/${route.params.id}`);
  tools.value = await api("/api/tools");
  servers.value = await api("/api/mcp");
  const current = agent.value.versions.at(-1);
  prompt.value = current?.system_prompt || "";
  selected.value = current?.tools || [];
  mcpIds.value = current?.mcp_ids || [];
  remoteProtocol.value = agent.value.remote_protocol || "http_sync";
  remoteEndpoint.value = agent.value.remote_endpoint || "";
  remoteToken.value = "";
  remoteTimeout.value = agent.value.remote_timeout_ms || 30000;
  remoteHealthUrl.value = agent.value.remote_health_url || "";
  remoteHealthMethod.value = agent.value.remote_health_method || "GET";
  gray.value = agent.value.gray_percent || 0;
}
const mcpLoads = {};
async function loadMcp(id) {
  if (mcpCatalog.value[id] && !mcpCatalog.value[id].loading) return;
  if (!mcpLoads[id]) {
    mcpCatalog.value = { ...mcpCatalog.value, [id]: { tools: [], loading: true } };
    mcpLoads[id] = api(`/api/mcp/${id}/tools`)
      .then((found) => {
        mcpCatalog.value = { ...mcpCatalog.value, [id]: { tools: found.tools || [] } };
      })
      .catch((err) => {
        mcpCatalog.value = { ...mcpCatalog.value, [id]: { tools: [], error: err.message } };
      })
      .finally(() => {
        delete mcpLoads[id];
      });
  }
  await mcpLoads[id];
}
async function writeGuide() {
  await Promise.all(mcpIds.value.map((id) => loadMcp(id)));
  const listed = Array.isArray(tools.value) ? tools.value : tools.value?.items || [];
  const listedServers = Array.isArray(servers.value) ? servers.value : servers.value?.items || [];
  prompt.value = applyGuide(
    prompt.value,
    guideText({
      tools: listed,
      toolNames: selected.value,
      servers: listedServers,
      mcpIds: mcpIds.value,
      catalog: mcpCatalog.value,
    }),
  );
}
onMounted(() => load().catch((err) => (error.value = err.message)));

async function toggle(name) {
  selected.value = selected.value.includes(name) ? selected.value.filter((item) => item !== name) : [...selected.value, name];
  await writeGuide();
}
function explain(message) {
  if (message === "没有灰度版本") return "还没有灰度版本。先点「发布但不放量」：已有线上版本时，最新草稿会停在灰度位且流量为 0，然后再填百分比点灰度。";
  if (message === "没有可回滚版本") return "没有可回滚的上一版。至少发布过两个版本后，回滚才会把线上流量退回上一版。";
  if (message === "评测未通过，不能发布") return "评测未通过，不能发布。请先到评测中心用这个智能体跑通评测。保存草稿不会改变线上正在使用的版本。";
  return message;
}
function place(version) {
  const names = [];
  if (version.number === agent.value.stable_version) names.push("线上");
  if (version.number === agent.value.gray_version) names.push(`灰度 ${agent.value.gray_percent}%`);
  if (latest.value && version.number === latest.value.number) names.push("正在编辑");
  return names.join("、") || "历史";
}
async function act(path, body, done) {
  error.value = "";
  notice.value = "";
  try {
    await api(path, { method: "POST", body });
    await load();
    notice.value = done;
  } catch (err) {
    error.value = explain(err.message);
  }
}
async function save() {
  error.value = "";
  notice.value = "";
  try {
    const body = isRemote.value
      ? {
          system_prompt: prompt.value,
          remote_protocol: remoteProtocol.value,
          remote_endpoint: remoteEndpoint.value,
          remote_timeout_ms: Number(remoteTimeout.value) || 30000,
          remote_health_url: remoteHealthUrl.value || "",
          remote_health_method: remoteHealthMethod.value || "GET",
          ...(remoteToken.value ? { remote_token: remoteToken.value } : {}),
        }
      : { system_prompt: prompt.value, tool_names: selected.value };
    await api(`/api/agents/${route.params.id}`, { method: "PATCH", body });
    await load();
    notice.value = `已写入 v${latest.value?.number}。线上仍是 ${agent.value.stable_version ? `v${agent.value.stable_version}` : "未发布"}。测试环境试运行用的是正在编辑的这一版。`;
  } catch (err) {
    error.value = explain(err.message);
  }
}
function publish() {
  if (latest.value && latest.value.status !== "published" && !latest.value.eval_passed) {
    error.value = explain("评测未通过，不能发布");
    notice.value = "";
    return;
  }
  act(`/api/agents/${agent.value.id}/publish`, { gray_percent: 0 }, "发布请求已完成。看版本表里标成「线上」或「灰度」的那一行。");
}
function shiftGray() {
  if (!agent.value.gray_version) {
    error.value = explain("没有灰度版本");
    notice.value = "";
    return;
  }
  act(`/api/agents/${agent.value.id}/gray`, { percent: Number(gray.value) }, `灰度流量已设为 ${Number(gray.value)}%。线上版本没有变，仍是 ${agent.value.stable_version ? `v${agent.value.stable_version}` : "未发布"}。`);
}
function promote(version, target) {
  const percent = Number(gray.value);
  const done = target === "stable"
    ? `v${version.number} 已设为线上。正式环境默认走这一版。`
    : `v${version.number} 已设为灰度，流量 ${percent}%。线上版本不变。`;
  act(`/api/agents/${agent.value.id}/versions/${version.number}/promote`, { target, percent }, done);
}
function resetDialogue() {
  thread.value = [];
  resumeTraceId.value = "";
}
async function run(continuing = false) {
  if (running.value) return;
  const text = continuing ? "" : message.value.trim();
  if (!continuing && !text) {
    error.value = "请填写输入";
    return;
  }
  error.value = "";
  running.value = true;
  const history = resumeTraceId.value
    ? []
    : thread.value.map((item) => ({ role: item.role, content: item.content }));
  const body = {
    message: text,
    environment: environment.value,
    history,
    resume_trace_id: resumeTraceId.value,
    stream: Boolean(useStream.value && !isRemote.value),
  };
  try {
    if (text) {
      thread.value.push({ role: "user", content: text });
      message.value = "";
    }
    if (body.stream) {
      thread.value.push({
        role: "assistant",
        content: "",
        pending: true,
        activity: "",
        toolCalls: [],
        status: "running",
        version: null,
        cost: 0,
        tokens: 0,
      });
      // 必须改数组里的响应式对象；直接改 push 前的 plain object 不会触发界面刷新
      const live = thread.value[thread.value.length - 1];
      await stream(`/api/agents/${route.params.id}/run`, body, (event) => {
        if (event.type === "reset") live.content = "";
        if (event.type === "delta") {
          live.content += event.text || "";
          live.activity = "";
        }
        if (event.type === "tool") live.activity = `正在调用 ${event.name}`;
        if (event.type === "error") throw new Error(event.message || "执行失败");
        if (event.type === "done" && event.result) {
          const payload = event.result;
          live.pending = false;
          live.content = payload.output || live.content;
          live.traceId = payload.trace_id;
          live.limited = Boolean(payload.limited);
          live.toolCalls = payload.tool_calls || [];
          live.status = payload.status;
          live.version = payload.version;
          live.cost = payload.cost;
          live.tokens = payload.tokens;
          live.activity = "";
          resumeTraceId.value = payload.limited ? payload.trace_id : "";
        }
      });
      return;
    }
    const payload = await api(`/api/agents/${route.params.id}/run`, {
      method: "POST",
      body,
    });
    thread.value.push({
      role: "assistant",
      content: payload.output,
      traceId: payload.trace_id,
      limited: Boolean(payload.limited),
      toolCalls: payload.tool_calls || [],
      status: payload.status,
      version: payload.version,
      cost: payload.cost,
      tokens: payload.tokens,
    });
    resumeTraceId.value = payload.limited ? payload.trace_id : "";
  } catch (err) {
    error.value = err.message;
    if (body.stream) {
      const last = thread.value[thread.value.length - 1];
      if (last?.pending) thread.value.pop();
      if (text && thread.value.at(-1)?.role === "user" && thread.value.at(-1)?.content === text) {
        thread.value.pop();
        message.value = text;
      }
    }
  } finally {
    running.value = false;
  }
}
</script>

<template>
  <p class="kicker">02 工作室</p>
  <div class="top">
    <div>
      <h1 v-if="agent">{{ agent.name }}</h1>
      <h1 v-else>工作室</h1>
      <p class="sub" v-if="agent && isRemote">远程智能体 · {{ agent.remote_protocol }}。工具与技能由远端自行提供，本机只转发与记轨迹。工作负载 {{ agent.workload_id }} · {{ agent.status }}</p>
      <p class="sub" v-else-if="agent">上方编辑的是最新一版。勾选工具后，提示词末尾的「可用工具」会跟着变化。标成「线上」的版本才接正式流量。工作负载 {{ agent.workload_id }} · {{ agent.status }}</p>
      <p class="sub" v-else>这里是单个智能体的运行页，不是列表。</p>
    </div>
    <router-link class="ghost" to="/agents">返回列表</router-link>
  </div>
  <p v-if="error" class="error">{{ error }}</p>
  <p v-if="notice" class="sub">{{ notice }}</p>
  <div class="grid-2" v-if="agent">
    <div class="sheet">
      <h2>配置</h2>
      <template v-if="isRemote">
        <label class="field"><span>协议</span>
          <select v-model="remoteProtocol">
            <option value="http_sync">同步 HTTP</option>
            <option value="http_async">异步 HTTP</option>
            <option value="a2a">A2A</option>
          </select>
        </label>
        <label class="field"><span>远端地址</span><input v-model="remoteEndpoint" /></label>
        <label class="field"><span>访问令牌</span><input v-model="remoteToken" :placeholder="agent.has_remote_token ? '已配置，留空则不改' : '可选'" /></label>
        <label class="field"><span>超时毫秒</span><input v-model.number="remoteTimeout" type="number" min="1000" /></label>
        <label class="field"><span>健康探测（可选）</span>
          <div class="row" style="gap: 8px; align-items: center">
            <select v-model="remoteHealthMethod" style="width: 96px">
              <option value="GET">GET</option>
              <option value="POST">POST</option>
            </select>
            <input v-model="remoteHealthUrl" placeholder="留空则总览无法实时监测" style="flex: 1" />
          </div>
        </label>
        <label class="field"><span>简介</span><textarea v-model="prompt" rows="5" placeholder="向本平台用户介绍职责、工具与适用场景" /></label>
        <p class="sub">简介给选型与总览使用。异步协议会先返回 waiting；A2A 可填 RPC 地址或根地址（读 Agent Card）。</p>
      </template>
      <template v-else>
        <label class="field"><span>系统提示词</span><textarea v-model="prompt" /></label>
        <p class="sub">勾选或取消工具后，上面的提示词会立即改写「可用工具」一段。这段之前的文字会保留。点保存草稿后才写入版本。</p>
        <div class="checks">
          <label v-for="tool in tools" :key="tool.id" class="check">
            <input type="checkbox" :checked="selected.includes(tool.name)" @change="toggle(tool.name)" />
            {{ tool.name }}
          </label>
        </div>
      </template>
      <p class="sub" v-if="latest">正在编辑 v{{ latest.number }}（{{ latest.status }}，评测 {{ latest.eval_passed ? "通过" : "未过" }}）。线上是 {{ agent.stable_version ? `v${agent.stable_version}` : "还没有" }}{{ agent.gray_version ? `，灰度是 v${agent.gray_version}（${agent.gray_percent}%）` : "" }}。试运行选 test 用正在编辑的版本，选 prod 用线上版本，命中灰度时走灰度版本。</p>
      <p class="sub">「调整灰度」只改当前灰度版本的流量比例。「恢复」只把已下线的智能体重新接上现在的线上版本。要换成某一版，在版本表里点「设为线上」。</p>
      <div class="row" style="margin-top: 14px">
        <button class="btn" type="button" @click="save">保存草稿</button>
        <button class="ghost" type="button" @click="publish">发布但不放量</button>
        <button class="ghost" type="button" @click="shiftGray">调整灰度</button>
        <input v-model="gray" type="number" min="0" max="100" style="width: 72px" aria-label="灰度百分比" />
      </div>
      <div class="row" style="margin-top: 10px">
        <button class="ghost" type="button" @click="act(`/api/agents/${agent.id}/rollback`, undefined, '已回滚到上一已发布版本。')">回滚</button>
        <button class="ghost" type="button" @click="act(`/api/agents/${agent.id}/offline`, undefined, '已下线。正式环境不再接流量。')">下线</button>
        <button class="ghost" type="button" @click="act(`/api/agents/${agent.id}/online`, undefined, '已恢复上线。')">恢复</button>
      </div>
      <h2 style="margin-top: 22px">版本</h2>
      <p class="sub">点「查看」看这一版的提示词、工具和模型。点「设为线上」后，正式环境默认走这一版。点「设为灰度」时使用上面输入的百分比。</p>
      <div class="table-scroll" tabindex="0" role="region" aria-label="版本列表">
      <table class="compact-table version-table">
        <colgroup>
          <col class="ver-no" />
          <col class="ver-place" />
          <col class="ver-status" />
          <col class="ver-eval" />
          <col />
          <col class="ver-ops" />
        </colgroup>
        <thead><tr><th>#</th><th>位置</th><th>状态</th><th>评测</th><th>{{ isRemote ? "简介" : "提示词" }}</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="version in agent.versions" :key="version.id" :class="{ 'version-live': version.number === agent.stable_version }">
            <td>v{{ version.number }}</td>
            <td>{{ place(version) }}</td>
            <td>{{ version.status }}</td>
            <td>{{ version.eval_passed ? "通过" : "未过" }}</td>
            <td :title="version.system_prompt">{{ version.system_prompt || "—" }}</td>
            <td class="version-actions">
              <button class="ghost" type="button" @click="viewed = version">查看</button>
              <button class="ghost" type="button" :disabled="version.number === agent.stable_version" @click="promote(version, 'stable')">设为线上</button>
              <button class="ghost" type="button" :disabled="version.number === agent.stable_version || version.number === agent.gray_version" @click="promote(version, 'gray')">设为灰度</button>
            </td>
          </tr>
        </tbody>
      </table>
      </div>
      <p class="sub" v-if="agent.references.length">仍被引用：{{ agent.references.map((item) => item.name).join("、") }}</p>
    </div>
    <div class="sheet">
      <h2>试运行</h2>
      <label class="field"><span>环境</span>
        <select v-model="environment"><option>test</option><option>staging</option><option>prod</option></select>
      </label>
      <label class="field"><span>输入</span><textarea v-model="message" placeholder="输入问题。再次执行时会带上当前对话。" /></label>
      <label v-if="!isRemote" class="check"><input type="checkbox" v-model="useStream" /> 流式输出（默认关闭，勾选后边生成边显示）</label>
      <p class="sub" v-if="isRemote">远程调用会记入轨迹。异步协议若返回 waiting，可到轨迹页等待回调完成后再看结果。</p>
      <p class="sub" v-else>一次执行最多 20 轮。如果最后一轮仍是工具调用，会停在上限，可以继续执行，已有对话和工具结果会一起送入。</p>
      <div class="row">
        <button class="btn" type="button" :disabled="running" @click="run(false)">{{ running ? "运行中" : "执行" }}</button>
        <button class="ghost" type="button" :disabled="running || !resumeTraceId" @click="run(true)">继续执行</button>
        <button class="ghost" type="button" :disabled="running || !thread.length" @click="resetDialogue">清空对话</button>
        <span class="run-state" :data-state="running ? 'running' : 'idle'" role="status" aria-live="polite">{{ running ? "运行中" : "未在运行" }}</span>
      </div>
      <div v-if="thread.length" class="dialogue">
        <article v-for="(item, index) in thread" :key="index" class="bubble" :class="[item.role, { limited: item.limited }]">
          <p class="kicker">{{ item.role === "user" ? "输入" : "回答" }}</p>
          <p v-if="item.role === 'assistant' && !item.pending" class="sub" :title="`${item.cost} 元 · ${item.tokens || 0} token`">状态 {{ item.status }} · v{{ item.version }} · 费用 {{ moneyText(item.cost) }} · {{ countText(item.tokens || 0) }} token</p>
          <p v-if="item.pending" class="sub">{{ item.activity || "生成中…" }}</p>
          <p class="bubble-text">{{ item.content }}</p>
          <article v-for="(call, callIndex) in item.toolCalls || []" :key="callIndex" class="tool-call">
            <strong>{{ call.name }}</strong>
            <span class="sub"> {{ durationText(call.latency_ms) }}</span>
            <p class="kicker">入参</p>
            <pre class="tool-io">{{ prettyJson(call.input) }}</pre>
            <p class="kicker">输出</p>
            <pre class="tool-io">{{ call.output || "—" }}</pre>
          </article>
          <router-link v-if="item.traceId" :to="`/traces/${item.traceId}`">查看轨迹</router-link>
        </article>
      </div>
    </div>
  </div>
  <Dialog :open="Boolean(viewed)" mode="view" :title="viewed ? `v${viewed.number} 详情` : '版本详情'" wide @close="viewed = null">
    <template v-if="viewed">
      <p class="sub">{{ place(viewed) }} · {{ viewed.status }} · 评测 {{ viewed.eval_passed ? "通过" : "未过" }}</p>
      <label class="field"><span>模型</span><input :value="viewed.model_name || '按任务自动选择'" readonly /></label>
      <label class="field"><span>工具</span><input :value="viewed.tools.join('、') || '无'" readonly /></label>
      <label class="field"><span>MCP</span><input :value="(viewed.mcp_names || []).join('、') || '无'" readonly /></label>
      <label class="field"><span>子智能体</span><input :value="(viewed.child_names || []).join('、') || '无'" readonly /></label>
      <label class="field"><span>系统提示词</span><textarea :value="viewed.system_prompt || '无'" readonly /></label>
    </template>
  </Dialog>
</template>
