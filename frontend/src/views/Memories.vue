<script setup>
import { computed, onMounted, ref } from "vue";
import { api, session } from "../api";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const desk = useDesk("/api/memories");
const open = ref(false);
const detailOpen = ref(false);
const mode = ref("create");
const platform = computed(() => Boolean(session().user?.platform_admin));
const form = ref(null);
const detail = ref(null);
const activating = ref(false);
desk.query.source_name = "";
desk.query.source_kind = "";
desk.query.kind = "";

const columns = computed(() => [
  { key: "kind_label", label: "类型", format: (value, row) => value || row.kind || "—" },
  {
    key: "level",
    label: "版本",
    format: (value, row) => {
      if (row.kind !== "user") return "—";
      const ver = value || 1;
      const extra = row.version_count > 1 ? ` / 共${row.version_count}版` : "";
      return `v${ver}${row.active ? "·启用" : ""}${extra}`;
    },
  },
  { key: "source_kind_label", label: "来源", format: (value, row) => value || row.source_kind || "—" },
  { key: "source_name", label: "会话/任务", format: (value) => value || "—" },
  { key: "content", label: "内容" },
  ...(platform.value
    ? [
        { key: "creator", label: "所属用户", format: (value) => value || "—" },
        { key: "department", label: "部门" },
      ]
    : []),
  { key: "created_at", label: "时间", format: when },
]);

function empty() {
  return { id: null, kind: "long", content: "" };
}
form.value = empty();

function openWith(row, next) {
  form.value = row ? { id: row.id, kind: row.kind, content: row.content } : empty();
  mode.value = next;
  open.value = true;
}

async function openDetail(row) {
  try {
    detail.value = await api(`/api/memories/${row.id}`);
    detailOpen.value = true;
  } catch (err) {
    desk.error.value = err.message;
  }
}

async function save() {
  try {
    if (mode.value === "create") {
      // 记忆归属当前登录用户与当前部门上下文，不选手动指定部门
      await api("/api/memories", {
        method: "POST",
        body: { kind: form.value.kind, content: form.value.content },
      });
    } else await api(`/api/memories/${form.value.id}`, { method: "PATCH", body: { content: form.value.content } });
    open.value = false;
    detailOpen.value = false;
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}

async function remove(row) {
  if (row.kind === "user" && row.active) {
    desk.error.value = "不能删除启用中的用户画像，请先启用其他版本";
    return;
  }
  if (!window.confirm(row.kind === "user" ? "删除该历史画像版本？" : "删除这条记忆？")) return;
  try {
    await api(`/api/memories/${row.id}`, { method: "DELETE" });
    if (detail.value?.id === row.id) detailOpen.value = false;
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}

async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条？`)) return;
  try {
    await api("/api/memories/bulk-delete", { method: "POST", body: { ids: desk.selected.value } });
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}

async function activateVersion(version) {
  if (!version?.id || version.active || activating.value) return;
  activating.value = true;
  try {
    const saved = await api(`/api/memories/${version.id}/activate`, { method: "POST", body: {} });
    detail.value = saved;
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  } finally {
    activating.value = false;
  }
}

function editFromDetail() {
  if (!detail.value) return;
  detailOpen.value = false;
  openWith(detail.value, "edit");
}

function editVersion(version) {
  if (!version?.id) return;
  detailOpen.value = false;
  openWith({ id: version.id, kind: "user", content: version.content }, "edit");
}

function roleLabel(role) {
  if (role === "user") return "用户";
  if (role === "assistant") return "助手";
  return role || "消息";
}

function kindLabel(kind) {
  return { short: "短期", long: "长期", user: "用户画像", summary: "摘要" }[kind] || kind || "记忆";
}

function hasLineage(detailRow) {
  const refs = detailRow?.detail?.source_refs || {};
  const produced = detailRow?.detail?.produced || {};
  return Boolean(
    (refs.dialogues || []).length
    || (refs.summaries || []).length
    || (refs.prior_short || []).length
    || (refs.prior_long || []).length
    || (refs.prior_user || []).length
    || (produced.long || []).length
    || (produced.user || []).length
    || (produced.notes || []).length
  );
}

async function download() {
  const data = await api("/api/memories/export");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "memories.json";
  link.click();
}

onMounted(() => desk.refresh());
</script>

<template>
  <Desk
    kicker="10 记忆"
    title="记忆"
    sub="记忆即摘要，分三类：短期（按会话/任务可多条）、长期（可多条，压缩时同主题更新或新增）、用户画像（每用户一条启用中版本，压缩调整时固化旧版并生成新版）。手动新增与压缩结果默认归属当前登录用户；选用某部门智能体时记忆落在该部门。平台管理员可跨部门查看本人记忆与全部用户画像。点击用户画像可管理版本。"
    :columns="columns"
    :rows="desk.pageData.value.items"
    :total="desk.pageData.value.total"
    :query="desk.query"
    :exact-fields="[
      { value: 'kind', label: '类型' },
      { value: 'source_name', label: '会话/任务名' },
      { value: 'content', label: '内容' },
    ]"
    :error="desk.error.value"
    :selected="desk.selected.value"
    @update:selected="desk.selected.value = $event"
    @load="desk.refresh()"
    @create="openWith(null, 'create')"
    @view="openDetail"
    @edit="openWith($event, 'edit')"
    @remove="remove"
    @bulk="bulk"
  >
    <template #filters>
      <label>来源
        <SearchSelect v-model="desk.query.source_kind">
          <option value="">全部</option>
          <option value="chat">问答</option>
          <option value="agent">智能体</option>
          <option value="workflow">工作流</option>
        </SearchSelect>
      </label>
      <label>类型
        <SearchSelect v-model="desk.query.kind">
          <option value="">全部</option>
          <option value="short">短期</option>
          <option value="long">长期</option>
          <option value="user">用户画像</option>
        </SearchSelect>
      </label>
      <label>会话/任务名<input v-model="desk.query.source_name" placeholder="按名称过滤" /></label>
      <button class="ghost" type="button" @click="download">导出</button>
    </template>
  </Desk>
  <Dialog :open="open" :mode="mode" :title="form?.kind === 'user' ? '用户画像' : '记忆'" @close="open = false" @submit="save">
    <label v-if="mode === 'create'" class="field"><span>类型</span>
      <SearchSelect v-model="form.kind">
        <option value="long">长期</option>
        <option value="short">短期</option>
        <option value="user">用户画像</option>
      </SearchSelect>
    </label>
    <p v-if="mode === 'edit' && form?.kind === 'user'" class="sub">保存后将固化当前版本，并启用编辑后的新版本。</p>
    <label class="field"><span>内容</span><textarea v-model="form.content" rows="8" /></label>
  </Dialog>
  <Dialog :open="detailOpen" mode="view" live :title="detail?.kind === 'user' ? '用户画像' : '记忆详情'" @close="detailOpen = false">
    <template v-if="detail">
      <p class="sub">
        {{ detail.kind_label }}
        <template v-if="detail.kind === 'user'"> · v{{ detail.level || 1 }}{{ detail.active ? ' · 启用中' : ' · 历史' }}</template>
        · {{ detail.source_kind_label || "—" }} · {{ detail.source_name || "—" }}
        <template v-if="platform && detail.creator"> · {{ detail.creator }}</template>
      </p>
      <label class="field"><span>内容</span><textarea :value="detail.content" readonly rows="6" /></label>
      <div class="row detail-actions">
        <button class="ghost" type="button" @click="editFromDetail">编辑</button>
        <button
          v-if="detail.kind === 'user' && !detail.active"
          class="btn"
          type="button"
          :disabled="activating"
          @click="activateVersion(detail)"
        >{{ activating ? "启用中…" : "启用此版本" }}</button>
      </div>

      <template v-if="detail.kind === 'user' && detail.detail?.versions?.length">
        <p class="kicker">画像版本</p>
        <ul class="version-list">
          <li v-for="item in detail.detail.versions" :key="item.id" :class="{ active: item.active }">
            <div class="version-head">
              <strong>v{{ item.level || 1 }}</strong>
              <span v-if="item.active" class="tag">启用中</span>
              <span class="muted">{{ item.created_at ? item.created_at.replace('T', ' ').slice(0, 16) : '' }}</span>
              <span class="muted">{{ item.source_name || '' }}</span>
            </div>
            <p class="version-body">{{ item.content }}</p>
            <div class="row version-actions">
              <button class="ghost" type="button" @click="openDetail(item)">查看</button>
              <button v-if="!item.active" class="ghost" type="button" :disabled="activating" @click="activateVersion(item)">启用</button>
              <button class="ghost" type="button" @click="editVersion(item)">编辑</button>
            </div>
          </li>
        </ul>
      </template>

      <template v-if="(detail.kind === 'short' || detail.kind === 'long') && hasLineage(detail)">
        <template v-if="detail.detail?.source_refs?.dialogues?.length">
          <p class="kicker">原始对话</p>
          <ul class="lineage-list">
            <li v-for="(turn, index) in detail.detail.source_refs.dialogues" :key="`d-${index}`">
              <span class="tag">{{ roleLabel(turn.role) }}</span>
              <p class="version-body">{{ turn.content }}</p>
            </li>
          </ul>
        </template>

        <template v-if="detail.detail?.source_refs?.summaries?.length">
          <p class="kicker">历史摘要</p>
          <ul class="memory-list">
            <li v-for="(text, index) in detail.detail.source_refs.summaries" :key="`s-${index}`">{{ text }}</li>
          </ul>
        </template>

        <template v-if="detail.detail?.source_refs?.prior_short?.length">
          <p class="kicker">历史短期记忆</p>
          <ul class="lineage-list">
            <li v-for="item in detail.detail.source_refs.prior_short" :key="`ps-${item.id || item.content}`">
              <div class="version-head">
                <span class="tag">{{ kindLabel(item.kind || 'short') }}</span>
                <span class="muted">{{ item.source_name || '' }}</span>
                <button v-if="item.id" class="ghost" type="button" @click="openDetail(item)">查看</button>
              </div>
              <p class="version-body">{{ item.content }}</p>
            </li>
          </ul>
        </template>

        <template v-if="detail.detail?.source_refs?.prior_long?.length">
          <p class="kicker">历史长期记忆</p>
          <ul class="lineage-list">
            <li v-for="item in detail.detail.source_refs.prior_long" :key="`pl-${item.id || item.content}`">
              <div class="version-head">
                <span class="tag">{{ kindLabel(item.kind || 'long') }}</span>
                <span class="muted">{{ item.source_name || '' }}</span>
                <button v-if="item.id" class="ghost" type="button" @click="openDetail(item)">查看</button>
              </div>
              <p class="version-body">{{ item.content }}</p>
            </li>
          </ul>
        </template>

        <template v-if="detail.kind === 'short' && detail.detail?.produced?.long?.length">
          <p class="kicker">本轮产生的长期记忆</p>
          <ul class="lineage-list">
            <li v-for="item in detail.detail.produced.long" :key="`out-l-${item.id}`">
              <div class="version-head">
                <span class="tag">长期</span>
                <button v-if="item.id" class="ghost" type="button" @click="openDetail(item)">查看</button>
              </div>
              <p class="version-body">{{ item.content }}</p>
            </li>
          </ul>
        </template>

        <template v-if="detail.kind === 'short' && detail.detail?.produced?.user?.length">
          <p class="kicker">本轮产生的用户画像</p>
          <ul class="lineage-list">
            <li v-for="item in detail.detail.produced.user" :key="`out-u-${item.id}`">
              <div class="version-head">
                <span class="tag">用户画像</span>
                <span v-if="item.level" class="muted">v{{ item.level }}</span>
                <button v-if="item.id" class="ghost" type="button" @click="openDetail(item)">查看</button>
              </div>
              <p class="version-body">{{ item.content }}</p>
            </li>
          </ul>
        </template>
      </template>

      <template v-else-if="detail.kind === 'long' || detail.kind === 'short'">
        <p class="kicker">说明</p>
        <p class="sub" v-if="detail.kind === 'long'">长期记忆可多条：压缩时同主题会更新，新主题会新增；支持编辑与删除。</p>
        <p class="sub" v-else>短期记忆按会话/任务产生，可多条，仅当前任务注入。</p>
      </template>

      <template v-if="detail.detail?.produced?.notes?.length">
        <p class="kicker">变更说明</p>
        <ul class="memory-list">
          <li v-for="(note, index) in detail.detail.produced.notes" :key="index">{{ note }}</li>
        </ul>
      </template>
    </template>
  </Dialog>
</template>

<style scoped>
.detail-actions { gap: 0.5rem; margin: 0.4rem 0 1rem; }
.version-list, .lineage-list { list-style: none; margin: 0.4rem 0 1rem; padding: 0; display: grid; gap: 0.75rem; }
.version-list li, .lineage-list li { border: 1px solid var(--line, #ddd); border-radius: 6px; padding: 0.65rem 0.75rem; }
.version-list li.active { border-color: var(--accent, #3b82f6); }
.version-head { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin-bottom: 0.35rem; }
.version-body { margin: 0; white-space: pre-wrap; font-size: 0.92rem; }
.version-actions { gap: 0.4rem; margin-top: 0.45rem; }
.tag { font-size: 0.75rem; padding: 0.1rem 0.4rem; border-radius: 999px; background: color-mix(in srgb, var(--accent, #3b82f6) 18%, transparent); }
.muted { opacity: 0.65; font-size: 0.8rem; }
.memory-list { margin: 0.3rem 0 1rem; padding-left: 1.1rem; }
.memory-list li { margin: 0.25rem 0; white-space: pre-wrap; }
</style>
