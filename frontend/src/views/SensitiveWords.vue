<script setup>
import { computed, onMounted, ref } from "vue";
import { api, session } from "../api";
import { useDesk, when } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const desk = useDesk("/api/sensitive-words");
const open = ref(false);
const mode = ref("create");
const platform = computed(() => Boolean(session().user?.platform_admin));
const spaces = computed(() => session().spaces || []);
const form = ref(empty());
const columns = computed(() => [
  { key: "name", label: "名称" },
  { key: "words_text", label: "敏感词", format: (value) => value || "—" },
  { key: "word_count", label: "词数" },
  { key: "enabled", label: "启用", format: (value) => (value ? "启用" : "停用") },
  ...(platform.value
    ? [
        { key: "creator", label: "创建者", format: (value) => value || "—" },
        { key: "department", label: "部门" },
      ]
    : []),
  { key: "created_at", label: "时间", format: when },
]);

function empty() {
  return {
    id: null,
    name: "",
    words_text: "",
    enabled: true,
    space_id: spaces.value[0]?.id || "",
  };
}
function openWith(row, next) {
  form.value = row
    ? {
        id: row.id,
        name: row.name,
        words_text: row.words_text || (row.words || []).join("、"),
        enabled: row.enabled !== false,
        space_id: row.space_id,
      }
    : empty();
  mode.value = next;
  open.value = true;
}
async function save() {
  try {
    const body = {
      name: form.value.name,
      words_text: form.value.words_text,
      enabled: Boolean(form.value.enabled),
    };
    if (mode.value === "create") {
      if (platform.value) body.space_id = Number(form.value.space_id);
      await api("/api/sensitive-words", { method: "POST", body });
    } else {
      await api(`/api/sensitive-words/${form.value.id}`, { method: "PATCH", body });
    }
    open.value = false;
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function remove(row) {
  if (!window.confirm(`删除敏感词清单「${row.name}」？`)) return;
  try {
    await api(`/api/sensitive-words/${row.id}`, { method: "DELETE" });
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条？`)) return;
  try {
    await api("/api/sensitive-words/bulk-delete", { method: "POST", body: { ids: desk.selected.value } });
    await desk.refresh();
  } catch (err) {
    desk.error.value = err.message;
  }
}
onMounted(() => desk.refresh());
</script>

<template>
  <Desk
    kicker="安全"
    title="敏感词"
    sub="一条清单可含多个敏感词（顿号、逗号或换行分隔）。启用后，智能体、工作流与模型问答的输入与输出（含流式）都会监测；命中则停止并提示触发的词。"
    :columns="columns"
    :rows="desk.pageData.value.items"
    :total="desk.pageData.value.total"
    :query="desk.query"
    :exact-fields="[{ value: 'name', label: '名称' }]"
    :error="desk.error.value"
    :selected="desk.selected.value"
    @update:selected="desk.selected.value = $event"
    @load="desk.refresh()"
    @create="openWith(null, 'create')"
    @view="openWith($event, 'view')"
    @edit="openWith($event, 'edit')"
    @remove="remove"
    @bulk="bulk"
  />
  <Dialog :open="open" :mode="mode" title="敏感词清单" @close="open = false" @submit="save">
    <label v-if="platform && mode === 'create'" class="field"><span>部门</span>
      <select v-model="form.space_id">
        <option v-for="item in spaces" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
    </label>
    <label class="field"><span>名称</span><input v-model="form.name" placeholder="例如：客服禁发词" /></label>
    <label class="field"><span>敏感词</span>
      <textarea v-model="form.words_text" rows="6" placeholder="多个词用顿号、逗号或换行分隔，例如：违禁品、内部价、未公开" />
    </label>
    <label class="check"><input type="checkbox" v-model="form.enabled" /> 启用监测</label>
    <p class="sub">命中后流式保留已输出正文，并追加「触发敏感词「…」，已停止输出」；非流式直接返回该说明，任务以拒绝结束。</p>
  </Dialog>
</template>
