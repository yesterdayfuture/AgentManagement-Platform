<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { api, session, upload } from "../api";
import { countText } from "../format";
import { useDesk, when, withScope, departmentNote } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const route = useRoute();
const tab = ref("bases");
const bases = useDesk(() => withScope(route, "/api/knowledge"));
const docs = useDesk("/api/knowledge/documents");
const baseDocs = useDesk("/api/knowledge/documents");
const open = ref(false);
const mode = ref("create");
const form = ref({});
const detailOpen = ref(false);
const detailBase = ref(null);
const hits = ref([]);
const queryText = ref("退款");
const platform = computed(() => Boolean(session().user?.platform_admin));
const spaces = computed(() => session().spaces || []);
const baseColumns = computed(() => [
  { key: "name", label: "名称" },
  { key: "document_count", label: "文档", format: (value) => countText(value) },
  ...(platform.value
    ? [
        { key: "creator", label: "创建者", format: (value) => value || "—" },
        { key: "department", label: "部门" },
      ]
    : []),
  { key: "created_at", label: "时间", format: when },
]);
const strategyLabels = { auto: "自动", heading: "按标题", paragraph: "按段落", sentence: "按句子", fixed: "固定长度" };
const docColumns = computed(() => [
  { key: "title", label: "标题" },
  { key: "source_kind", label: "来源" },
  { key: "applied_strategy", label: "切分", format: (value) => strategyLabels[value] || value },
  { key: "chunk_count", label: "切块", format: (value) => countText(value) },
  { key: "visibility", label: "可见性" },
  ...(platform.value ? [{ key: "department", label: "部门" }] : []),
  { key: "created_at", label: "时间", format: when },
]);
const active = () => (tab.value === "bases" ? bases : docs);

function openBase(row, next) {
  form.value = { kind: "base", ...(row ? { ...row } : { name: "", space_id: spaces.value[0]?.id || "" }) };
  mode.value = next;
  open.value = true;
}
async function openDetail(row) {
  detailBase.value = row;
  baseDocs.query.page = 1;
  baseDocs.query.q = "";
  baseDocs.query.exact_field = "";
  baseDocs.query.exact = "";
  baseDocs.query.base_id = row.id;
  baseDocs.query.time_from = "";
  baseDocs.query.time_to = "";
  detailOpen.value = true;
  await baseDocs.refresh();
}
function blankDoc(baseId) {
  return {
    kind: "doc",
    base_id: baseId || detailBase.value?.id || bases.pageData.value.items[0]?.id || "",
    title: "",
    content: "",
    visibility: "space",
    allowed_roles: "",
    source_name: "",
    source_kind: "",
    strategy: "auto",
    suggested: "",
    applied: "",
    chunk_size: 500,
    chunk_overlap: 40,
    chunks: [],
    chunk_count: 0,
  };
}
function openDoc(row, next, baseId) {
  form.value = row
    ? { ...blankDoc(row.base_id), ...row, kind: "doc", allowed_roles: (row.allowed_roles || []).join(","), chunks: [] }
    : blankDoc(baseId);
  mode.value = next;
  open.value = true;
  if (row?.content) preview();
}
async function onFile(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    docs.error.value = "";
    const parsed = await upload("/api/knowledge/parse", body);
    form.value.title = form.value.title || parsed.title;
    form.value.content = parsed.content;
    form.value.source_name = parsed.source_name;
    form.value.source_kind = parsed.source_kind;
    form.value.suggested = parsed.suggested;
    form.value.strategy = parsed.suggested;
    await preview();
  } catch (err) {
    docs.error.value = err.message;
  }
}
async function preview() {
  if (!form.value.content) return;
  try {
    const data = await api("/api/knowledge/preview", {
      method: "POST",
      body: {
        content: form.value.content,
        strategy: form.value.strategy,
        chunk_size: Number(form.value.chunk_size),
        chunk_overlap: Number(form.value.chunk_overlap),
      },
    });
    form.value.applied = data.applied;
    form.value.chunk_count = data.count;
    form.value.chunks = data.chunks;
  } catch (err) {
    docs.error.value = err.message;
  }
}
async function save() {
  const writingDoc = form.value.kind === "doc";
  const desk = writingDoc ? (detailOpen.value ? baseDocs : docs) : bases;
  try {
    if (!writingDoc) {
      if (mode.value === "create") await api("/api/knowledge", { method: "POST", body: { name: form.value.name, ...(platform.value ? { space_id: Number(form.value.space_id) } : {}) } });
      else await api(`/api/knowledge/${form.value.id}`, { method: "PATCH", body: { name: form.value.name } });
    } else {
      const body = {
        title: form.value.title,
        content: form.value.content,
        visibility: form.value.visibility,
        allowed_roles: String(form.value.allowed_roles || "").split(",").map((item) => item.trim()).filter(Boolean),
        source_name: form.value.source_name || "",
        source_kind: form.value.source_kind || "text",
        strategy: form.value.strategy || "auto",
        chunk_size: Number(form.value.chunk_size),
        chunk_overlap: Number(form.value.chunk_overlap),
      };
      if (!body.content.trim()) throw new Error("请先上传文件");
      if (mode.value === "create") await api(`/api/knowledge/${form.value.base_id}/documents`, { method: "POST", body });
      else await api(`/api/knowledge/documents/${form.value.id}`, { method: "PATCH", body });
    }
    open.value = false;
    await desk.refresh();
    if (writingDoc) {
      await docs.refresh();
      await bases.refresh();
      if (detailBase.value) {
        const fresh = bases.pageData.value.items.find((item) => item.id === detailBase.value.id);
        if (fresh) detailBase.value = fresh;
      }
    }
  } catch (err) { desk.error.value = err.message; }
}
async function remove(row) {
  const fromBase = Boolean(detailOpen.value && row.base_id);
  const desk = fromBase ? baseDocs : active();
  if (!window.confirm("删除这条记录？")) return;
  const path = fromBase || tab.value === "docs" ? `/api/knowledge/documents/${row.id}` : `/api/knowledge/${row.id}`;
  try {
    await api(path, { method: "DELETE" });
    await desk.refresh();
    if (fromBase) {
      await docs.refresh();
      await bases.refresh();
    }
  } catch (err) { desk.error.value = err.message; }
}
async function bulk(fromBase = false) {
  const desk = fromBase ? baseDocs : active();
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条？`)) return;
  const path = !fromBase && tab.value === "bases" ? "/api/knowledge/bulk-delete" : "/api/knowledge/documents/bulk-delete";
  try {
    await api(path, { method: "POST", body: { ids: desk.selected.value } });
    await desk.refresh();
    if (fromBase) {
      await docs.refresh();
      await bases.refresh();
    }
  } catch (err) { desk.error.value = err.message; }
}
async function search() {
  try { hits.value = (await api("/api/knowledge/search", { method: "POST", body: { query: queryText.value } })).hits; } catch (err) { docs.error.value = err.message; }
}
onMounted(async () => { await bases.refresh(); await docs.refresh(); });
watch(() => route.query.usable_space, () => bases.refresh());
</script>

<template>
  <div class="tabs">
    <button class="ghost" type="button" :aria-selected="tab === 'bases'" @click="tab = 'bases'">知识库</button>
    <button class="ghost" type="button" :aria-selected="tab === 'docs'" @click="tab = 'docs'">文档</button>
  </div>
  <Desk v-if="tab === 'bases'" kicker="05 知识" title="知识库" :sub="`点详情查看这个库里的文档，可以预览正文，也可以新增、修改和删除。${departmentNote(route)}`" view-label="详情" :columns="baseColumns" :rows="bases.pageData.value.items" :total="bases.pageData.value.total" :query="bases.query" :exact-fields="[{ value: 'name', label: '名称' }]" :error="bases.error.value" :selected="bases.selected.value" @update:selected="bases.selected.value = $event" @load="bases.refresh()" @create="openBase(null, 'create')" @view="openDetail" @edit="openBase($event, 'edit')" @remove="remove" @bulk="bulk()" />
  <Desk v-else kicker="05 知识" title="文档" sub="上传文件后自动抽出正文，再按标题、段落、句子或固定长度切分。" :columns="docColumns" :rows="docs.pageData.value.items" :total="docs.pageData.value.total" :query="docs.query" :exact-fields="[{ value: 'title', label: '标题' }, { value: 'visibility', label: '可见性' }, { value: 'base_id', label: '库编号' }]" :error="docs.error.value" :selected="docs.selected.value" @update:selected="docs.selected.value = $event" @load="docs.refresh()" @reset="queryText = ''; hits = []" @create="openDoc(null, 'create')" @view="openDoc($event, 'view')" @edit="openDoc($event, 'edit')" @remove="remove" @bulk="bulk()">
    <template #filters>
      <label>检索<input v-model="queryText" /></label>
      <button class="ghost" type="button" @click="search">检索</button>
    </template>
  </Desk>
  <div class="sheet" v-if="hits.length">
    <article class="pipe" v-for="hit in hits" :key="hit.document_id + hit.excerpt"><strong>{{ hit.title }}</strong><p>{{ hit.excerpt }}</p></article>
  </div>
  <Dialog wide live mode="view" :open="detailOpen" :title="detailBase?.name || '知识库'" @close="detailOpen = false">
    <p class="sub">共 {{ detailBase?.document_count ?? baseDocs.pageData.value.total }} 篇。预览看正文，编辑会重新切分。</p>
    <Desk kicker="" title="" sub="" create-label="新增文档" view-label="预览" empty-text="这个知识库还没有文档。" :columns="docColumns" :rows="baseDocs.pageData.value.items" :total="baseDocs.pageData.value.total" :query="baseDocs.query" :exact-fields="[{ value: 'title', label: '标题' }, { value: 'visibility', label: '可见性' }]" :error="baseDocs.error.value" :selected="baseDocs.selected.value" @update:selected="baseDocs.selected.value = $event" @load="baseDocs.refresh()" @create="openDoc(null, 'create', detailBase?.id)" @view="openDoc($event, 'view')" @edit="openDoc($event, 'edit')" @remove="remove" @bulk="bulk(true)" />
  </Dialog>
  <Dialog :open="open" :mode="mode" :title="form.name || form.title || '知识'" @close="open = false" @submit="save">
    <template v-if="form.kind !== 'doc'">
      <label v-if="platform && mode === 'create'" class="field"><span>部门</span>
        <select v-model="form.space_id"><option v-for="item in spaces" :key="item.id" :value="item.id">{{ item.name }}</option></select>
      </label>
      <label class="field"><span>名称</span><input v-model="form.name" /></label>
    </template>
    <template v-else>
      <label class="field"><span>知识库</span>
        <select v-model="form.base_id" :disabled="mode !== 'create' || detailOpen">
          <option v-for="item in bases.pageData.value.items" :key="item.id" :value="item.id">{{ item.name }}</option>
        </select>
      </label>
      <label class="field"><span>文件</span><input type="file" accept=".txt,.md,.markdown,.csv,.json,.html,.htm,.docx,.pdf,.xml,.log" @change="onFile" /></label>
      <p class="sub" v-if="form.source_name">{{ form.source_name }} · {{ form.source_kind }}<template v-if="form.suggested"> · 建议{{ strategyLabels[form.suggested] }}</template></p>
      <label class="field"><span>标题</span><input v-model="form.title" /></label>
      <label class="field"><span>解析正文</span><textarea v-model="form.content" /></label>
      <label class="field"><span>切分策略</span>
        <select v-model="form.strategy" @change="preview">
          <option v-for="(label, value) in strategyLabels" :key="value" :value="value">{{ label }}</option>
        </select>
      </label>
      <label class="field"><span>切块长度</span><input v-model.number="form.chunk_size" type="number" min="40" max="4000" @change="preview" /></label>
      <label class="field"><span>重叠长度</span><input v-model.number="form.chunk_overlap" type="number" min="0" max="2000" @change="preview" /></label>
      <label class="field"><span>可见性</span><select v-model="form.visibility"><option value="space">空间内</option><option value="restricted">受限</option></select></label>
      <label class="field"><span>受限角色，逗号分隔</span><input v-model="form.allowed_roles" placeholder="builder,admin" /></label>
    </template>
    <template #footer>
      <div v-if="form.kind === 'doc' && mode === 'view'" class="sheet">
        <p class="sub">正文</p>
        <p>{{ form.content || "没有正文。" }}</p>
      </div>
      <div v-if="form.kind === 'doc' && form.chunks?.length" class="sheet">
        <p class="sub">{{ strategyLabels[form.applied] || form.applied }} · {{ form.chunk_count }} 块</p>
        <article class="pipe" v-for="chunk in form.chunks" :key="chunk.position"><strong>#{{ chunk.position + 1 }}</strong><p>{{ chunk.text }}</p></article>
      </div>
    </template>
  </Dialog>
</template>
