<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { api, stream } from "../api";
import { countText, durationText, moneyText, prettyJson } from "../format";
import { renderMarkdown } from "../markdown";
import Dialog from "../components/Dialog.vue";

const sessions = ref([]);
const agents = ref([]);
const role = ref("");
const platformAdmin = ref(false);
const query = ref("");
const draft = ref("");
const activeId = ref("");
const thread = ref(null);
const sending = ref(false);
const composeMode = ref("chat");
const error = ref("");
const side = ref(Number(localStorage.getItem("ask-side") || 292));
const dragging = ref(false);
const renamingId = ref("");
const titleDraft = ref("");
const selectedAgent = ref("");
const menuId = ref("");
const selecting = ref(false);
const picked = ref([]);
const logEl = ref(null);
const route = useRoute();
const editingDraft = ref(null);
const draftSaving = ref(false);

const canSend = computed(() => ["operator", "builder", "admin"].includes(role.value));
const pinnedSessions = computed(() => sessions.value.filter((item) => item.pinned));
const otherSessions = computed(() => sessions.value.filter((item) => !item.pinned));
const lastLimited = computed(() => {
  const messages = thread.value?.messages || [];
  const last = messages[messages.length - 1];
  return Boolean(last && last.role === "assistant" && last.limited);
});

async function loadAgents() {
  const rows = await api("/api/agents");
  const list = Array.isArray(rows) ? rows : rows.items || [];
  agents.value = list;
}
async function loadSessions() {
  const q = query.value.trim();
  sessions.value = await api(`/api/chats${q ? `?q=${encodeURIComponent(q)}` : ""}`);
}
async function openSession(id) {
  error.value = "";
  activeId.value = id;
  renamingId.value = "";
  menuId.value = "";
  thread.value = await api(`/api/chats/${id}`);
  if (thread.value.agent_id) selectedAgent.value = String(thread.value.agent_id);
}
function startNew() {
  activeId.value = "";
  thread.value = null;
  draft.value = "";
  error.value = "";
  renamingId.value = "";
  menuId.value = "";
}
async function chooseAgent(event) {
  const agentId = Number(event.target.value);
  if (!agentId) return;
  selectedAgent.value = String(agentId);
  if (!thread.value) return;
  thread.value = { ...thread.value, agent_id: agentId };
  try {
    const saved = await api(`/api/chats/${thread.value.id}`, { method: "PATCH", body: { agent_id: agentId } });
    thread.value = { ...thread.value, ...saved };
    await loadSessions();
  } catch (err) {
    error.value = err.message;
  }
}
function canManage(item) {
  return canSend.value && (item.mine || role.value === "admin" || platformAdmin.value);
}
function toggleMenu(id) {
  menuId.value = menuId.value === id ? "" : id;
}
function closeMenu() {
  menuId.value = "";
}
async function send(continuing = false) {
  if (sending.value || !canSend.value) return;
  const text = continuing ? "" : draft.value.trim();
  if (!continuing && !text) return;
  const agentId = Number(selectedAgent.value || thread.value?.agent_id);
  if (!agentId) {
    error.value = "请选择智能体";
    return;
  }
  error.value = "";
  sending.value = true;
  if (!continuing) draft.value = "";
  const localUser = text ? { id: `local-user-${Date.now()}`, role: "user", content: text } : null;
  const localBot = { id: `local-bot-${Date.now()}`, role: "assistant", content: "", pending: true, tool_calls: [], activity: "" };
  if (!thread.value) {
    thread.value = { id: "", title: (text || "新的问答").slice(0, 24), agent_id: agentId, messages: [] };
  }
  if (localUser) thread.value.messages.push(localUser);
  thread.value.messages.push(localBot);
  const liveBot = thread.value.messages[thread.value.messages.length - 1];
  try {
    let sessionId = thread.value.id || "";
    if (!sessionId) {
      const created = await api("/api/chats", { method: "POST", body: { agent_id: agentId, title: (text || "").slice(0, 24) } });
      sessionId = created.id;
      thread.value = { ...created, messages: thread.value.messages };
      activeId.value = sessionId;
    }
    if (composeMode.value === "workflow" && !continuing) {
      const saved = await api(`/api/chats/${sessionId}/workflow`, { method: "POST", body: { message: text } });
      thread.value = saved;
      activeId.value = saved.id;
      await loadSessions();
      return;
    }
    await stream(`/api/chats/${sessionId}/messages`, { message: text, continue_run: continuing, stream: true }, (event) => {
      if (event.type === "reset") liveBot.content = "";
      if (event.type === "delta") {
        liveBot.content += event.text || "";
        liveBot.activity = "";
      }
      if (event.type === "tool") liveBot.activity = `正在调用 ${event.name}`;
      if (event.type === "error") throw new Error(event.message || "回答失败");
      if (event.type === "done" && event.session) {
        thread.value = event.session;
        activeId.value = event.session.id;
      }
    });
    await loadSessions();
  } catch (err) {
    error.value = err.message;
    if (thread.value?.messages) {
      thread.value.messages = thread.value.messages.filter((item) => item.id !== localBot.id);
      if (!thread.value.id && !thread.value.messages.length) thread.value = null;
    }
  } finally {
    sending.value = false;
  }
}
function onComposerKey(event) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send(false);
  }
}
function metaLine(item) {
  if (item.role !== "assistant" || item.pending) return "";
  const parts = [];
  if (item.version != null) parts.push(`版本 v${item.version}`);
  if (item.tool_count) parts.push(`工具 ${item.tool_count}`);
  else if (item.tool_calls?.length) parts.push(`工具 ${item.tool_calls.length}`);
  if (item.tokens) parts.push(`token ${countText(item.tokens)}`);
  if (item.cost) parts.push(`花费 ${moneyText(item.cost)}`);
  if (item.stop_reason) parts.push(item.stop_reason);
  return parts.join(" · ");
}
function beginEditDraft(item) {
  const draft = item.workflow_draft || {};
  const definition = draft.definition || {};
  editingDraft.value = {
    approval_id: item.approval_id,
    name: draft.name || "",
    kind: definition.kind || "agents",
    nodesText: JSON.stringify(definition.nodes || [], null, 2),
    edgesText: JSON.stringify(definition.edges || [], null, 2),
  };
}
function closeDraftEdit() {
  editingDraft.value = null;
}
async function saveDraftEdit() {
  if (!editingDraft.value?.approval_id) return;
  draftSaving.value = true;
  error.value = "";
  try {
    const nodes = JSON.parse(editingDraft.value.nodesText || "[]");
    const edges = JSON.parse(editingDraft.value.edgesText || "[]");
    if (!Array.isArray(nodes) || !nodes.length) throw new Error("至少保留一个节点");
    await api(`/api/approvals/${editingDraft.value.approval_id}`, {
      method: "PATCH",
      body: {
        name: editingDraft.value.name,
        definition: {
          kind: editingDraft.value.kind === "autonomous" ? "autonomous" : "agents",
          entry: nodes[0]?.id || "n1",
          nodes,
          edges: Array.isArray(edges) ? edges : [],
        },
      },
    });
    closeDraftEdit();
    if (activeId.value) await openSession(activeId.value);
  } catch (err) {
    error.value = err.message;
  } finally {
    draftSaving.value = false;
  }
}
async function removeSession(id) {
  error.value = "";
  try {
    await api(`/api/chats/${id}`, { method: "DELETE" });
    if (activeId.value === id) startNew();
    await loadSessions();
  } catch (err) {
    error.value = err.message;
  }
}
function beginRename(item) {
  menuId.value = "";
  renamingId.value = item.id;
  titleDraft.value = item.title;
}
async function saveTitle(item) {
  const title = titleDraft.value.trim();
  if (!title) {
    error.value = "标题不能为空";
    return;
  }
  try {
    const saved = await api(`/api/chats/${item.id}`, { method: "PATCH", body: { title } });
    if (thread.value?.id === item.id) thread.value = { ...thread.value, ...saved };
    renamingId.value = "";
    await loadSessions();
  } catch (err) {
    error.value = err.message;
  }
}
async function togglePin(item) {
  menuId.value = "";
  try {
    await api(`/api/chats/${item.id}`, { method: "PATCH", body: { pinned: !item.pinned } });
    await loadSessions();
  } catch (err) {
    error.value = err.message;
  }
}
async function shareSession(item) {
  menuId.value = "";
  const url = `${location.origin}/ask?chat=${item.id}`;
  try {
    await navigator.clipboard.writeText(url);
  } catch {
    error.value = "复制链接没有成功";
  }
}
function startPicking() {
  menuId.value = "";
  selecting.value = true;
  picked.value = [];
}
function togglePick(id) {
  picked.value = picked.value.includes(id) ? picked.value.filter((item) => item !== id) : [...picked.value, id];
}
async function removePicked() {
  error.value = "";
  try {
    for (const id of picked.value) {
      await api(`/api/chats/${id}`, { method: "DELETE" });
      if (activeId.value === id) startNew();
    }
    picked.value = [];
    selecting.value = false;
    await loadSessions();
  } catch (err) {
    error.value = err.message;
    await loadSessions();
  }
}
function startDrag(event) {
  dragging.value = true;
  const startX = event.clientX;
  const startWidth = side.value;
  function move(next) {
    const width = Math.min(460, Math.max(220, startWidth + next.clientX - startX));
    side.value = width;
  }
  function stop() {
    dragging.value = false;
    localStorage.setItem("ask-side", String(side.value));
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", stop);
  }
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", stop);
}
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text || "");
  } catch {
    error.value = "复制没有成功";
  }
}
watch(
  () => (thread.value?.messages || []).map((item) => `${item.id}:${item.content}`).join("\n"),
  async () => {
    await nextTick();
    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight;
  },
);
onMounted(async () => {
  document.addEventListener("click", closeMenu);
  try {
    const space = await api("/api/spaces/current");
    role.value = space.role;
    platformAdmin.value = Boolean(space.platform_admin);
    await Promise.all([loadAgents(), loadSessions()]);
    if (route.query.chat) await openSession(String(route.query.chat));
  } catch (err) {
    error.value = err.message;
  }
});
onUnmounted(() => document.removeEventListener("click", closeMenu));
</script>

<template>
  <div class="ask-page" :class="{ dragging }" :style="{ '--ask-side': `${side}px` }">
    <aside class="ask-index" aria-label="会话">
      <div class="ask-index-head">
        <div>
          <p class="kicker">问答</p>
          <h1>模型问答</h1>
        </div>
        <button v-if="selecting" class="ghost" type="button" @click="selecting = false">取消</button>
        <button v-else class="btn" type="button" :disabled="!canSend" @click="startNew">新对话</button>
      </div>
      <label class="field ask-search">
        <span>搜索问题</span>
        <input v-model="query" placeholder="按问题或标题查找" @input="loadSessions" />
        <button class="ghost" type="button" @click="query = ''; loadSessions()">重置</button>
      </label>
      <div class="ask-scroll">
      <p v-if="!sessions.length" class="sub">还没有匹配的会话。</p>
      <div v-if="selecting" class="ask-pick-bar">
        <span>已选 {{ picked.length }} 条</span>
        <button class="ghost danger" type="button" :disabled="!picked.length" @click="removePicked">删除所选</button>
      </div>
      <template v-for="group in [{ label: '置顶', rows: pinnedSessions }, { label: '对话', rows: otherSessions }]" :key="group.label">
        <p v-if="group.rows.length" class="ask-group">{{ group.label }}</p>
        <ul v-if="group.rows.length" class="ask-list">
          <li v-for="item in group.rows" :key="item.id">
            <label v-if="selecting" class="ask-check">
              <input type="checkbox" :checked="picked.includes(item.id)" @change="togglePick(item.id)" />
            </label>
            <input v-if="renamingId === item.id" class="ask-rename" v-model="titleDraft" @click.stop @keydown.enter.prevent="saveTitle(item)" @keydown.esc="renamingId = ''" />
            <button v-else class="ask-item" type="button" :aria-current="item.id === activeId ? 'true' : undefined" @click="openSession(item.id)">
              <strong>{{ item.title }}</strong>
              <small>{{ item.agent_name || "未选智能体" }}<template v-if="platformAdmin || role === 'admin'"> · {{ item.owner }}</template></small>
              <em v-if="item.snippet">{{ item.snippet }}</em>
            </button>
            <button v-if="canManage(item) && renamingId !== item.id" class="ask-more" type="button" aria-label="会话操作" :aria-expanded="menuId === item.id" @click.stop="toggleMenu(item.id)">···</button>
            <div v-if="menuId === item.id" class="ask-menu" role="menu" @click.stop>
              <button type="button" @click="beginRename(item)">重命名</button>
              <button type="button" @click="togglePin(item)">{{ item.pinned ? "取消置顶" : "置顶" }}</button>
              <button type="button" @click="shareSession(item)">分享</button>
              <button type="button" @click="startPicking">多选</button>
              <button class="danger" type="button" @click="removeSession(item.id)">删除</button>
            </div>
          </li>
        </ul>
      </template>
      </div>
    </aside>
    <div class="ask-gutter" role="separator" tabindex="0" aria-orientation="vertical" aria-label="调整会话栏宽度" @pointerdown="startDrag"></div>
    <section class="ask-sheet">
      <header class="ask-bar">
        <label class="field">
          <span>智能体</span>
          <select :value="selectedAgent" :disabled="!canSend" @change="chooseAgent">
            <option value="">{{ agents.length ? "请选择智能体" : "当前部门没有智能体" }}</option>
            <option v-for="agent in agents" :key="agent.id" :value="agent.id">{{ agent.name }}<template v-if="platformAdmin"> · {{ agent.department || "平台" }}</template></option>
          </select>
        </label>
        <div class="ask-title" v-if="thread">
          <p class="ask-title-button">{{ thread.title }}</p>
        </div>
        <p v-else class="sub">选择智能体后直接提问。构建者使用最新草稿，操作者使用已发布版本。</p>
      </header>
      <p v-if="error" class="error">{{ error }}</p>
      <div ref="logEl" class="ask-log" aria-live="polite">
        <p v-if="!thread" class="ask-empty">从左侧打开一条会话，或在下面写下第一个问题。</p>
        <article v-for="item in thread?.messages || []" :key="item.id" class="ask-turn" :class="item.role">
          <p class="kicker">{{ item.role === "user" ? "问题" : "回答" }}</p>
          <p v-if="item.activity" class="ask-activity">{{ item.activity }}</p>
          <div v-if="item.role === 'assistant'" class="md" v-html="renderMarkdown(item.content)"></div>
          <span v-if="item.pending" class="ask-caret" aria-hidden="true"></span>
          <p v-if="item.role === 'user'" class="ask-user-text">{{ item.content }}</p>
          <p v-if="metaLine(item)" class="ask-meta">{{ metaLine(item) }}</p>
          <div v-if="item.tool_calls?.length" class="ask-tools">
            <details v-for="(call, index) in item.tool_calls" :key="index">
              <summary>{{ call.name }} · {{ durationText(call.latency_ms) }}</summary>
              <p class="kicker">入参</p>
              <pre class="tool-io">{{ prettyJson(call.input) }}</pre>
              <p class="kicker">输出</p>
              <pre class="tool-io">{{ call.output || "—" }}</pre>
            </details>
          </div>
          <div v-if="item.workflow_draft" class="ask-draft">
            <p class="kicker">工作流草案 · {{ item.approval_status === "pending" ? "待审批" : item.approval_status }}</p>
            <p class="sub">{{ item.workflow_draft.name }} · {{ item.workflow_draft.summary }}</p>
            <div class="ask-turn-actions">
              <button v-if="item.approval_status === 'pending'" class="ghost" type="button" @click.stop="beginEditDraft(item)">改草案</button>
              <router-link v-if="item.approval_id" :to="`/approvals`">去审批 #{{ item.approval_id }}</router-link>
              <router-link v-if="item.workflow_id" :to="{ path: '/workflows', query: { open: String(item.workflow_id), task: item.workflow_draft.task || '' } }">打开工作流</router-link>
            </div>
          </div>
          <div class="ask-turn-actions" v-if="item.role === 'assistant'">
            <button class="ghost" type="button" @click="copyText(item.content)">复制</button>
            <router-link v-if="item.trace_id" :to="`/traces/${item.trace_id}`">查看轨迹</router-link>
          </div>
        </article>
      </div>
      <Dialog v-if="editingDraft" wide :open="true" mode="edit" :title="`编辑草案 #${editingDraft.approval_id}`" @close="closeDraftEdit" @submit="saveDraftEdit">
        <label class="field"><span>名称</span><input v-model="editingDraft.name" /></label>
        <label class="field"><span>执行方式</span>
          <select v-model="editingDraft.kind">
            <option value="agents">固定步骤</option>
            <option value="autonomous">自主调度</option>
          </select>
        </label>
        <label class="field"><span>节点 JSON</span><textarea v-model="editingDraft.nodesText" rows="10" /></label>
        <label class="field"><span>连线 JSON</span><textarea v-model="editingDraft.edgesText" rows="5" /></label>
        <p v-if="draftSaving" class="sub">保存中…</p>
      </Dialog>
      <footer class="ask-composer">
        <p v-if="!canSend" class="sub">只读账号可以查看和搜索会话，不能发起问答。</p>
        <div class="row">
          <button class="ghost ask-mode" type="button" :aria-pressed="composeMode === 'chat'" :disabled="!canSend || sending" @click="composeMode = 'chat'">提问</button>
          <button class="ghost ask-mode" type="button" :aria-pressed="composeMode === 'workflow'" :disabled="!canSend || sending" @click="composeMode = 'workflow'">生成工作流</button>
        </div>
        <textarea v-model="draft" :disabled="!canSend || sending" :placeholder="composeMode === 'workflow' ? '写下任务，模型会起草工作流。通过审批后才会出现在工作流管理。' : '输入问题，Enter 发送，Shift+Enter 换行'" @keydown="onComposerKey"></textarea>
        <div class="row">
          <button class="btn" type="button" :disabled="!canSend || sending || !selectedAgent" @click="send(false)">{{ sending ? (composeMode === "workflow" ? "起草中" : "回答中") : (composeMode === "workflow" ? "生成并送审" : "发送") }}</button>
          <button class="ghost" type="button" :disabled="!canSend || sending || !lastLimited" @click="send(true)">继续执行</button>
        </div>
      </footer>
    </section>
  </div>
</template>
