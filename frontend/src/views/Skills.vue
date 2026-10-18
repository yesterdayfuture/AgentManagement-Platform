<script setup>
import { onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { api, upload } from "../api";
import { useDesk, when, withScope, departmentNote } from "../useDesk";
import Desk from "../components/Desk.vue";
import Dialog from "../components/Dialog.vue";

const route = useRoute();
const desk = useDesk(() => withScope(route, "/api/skills"));
const zipInput = ref(null);
const open = ref(false);
const mode = ref("create");
const form = ref(empty());
const columns = [
  { key: "name", label: "名称" },
  { key: "key", label: "标识" },
  { key: "description", label: "何时使用" },
  { key: "scripts", label: "脚本", format: (value) => (value || []).join("、") || "—" },
  { key: "enabled", label: "启用", format: (value) => (value ? "启用" : "停用") },
  { key: "created_at", label: "时间", format: when },
];
function empty() {
  return { id: null, name: "", key: "", description: "", body: "", script_name: "run.py", script_source: "print('ok')\n", enabled: true, scripts: [], sources: {}, storage_path: "" };
}
function openWith(row, next) {
  form.value = row
    ? { id: row.id, name: row.name, key: row.key, description: row.description, body: row.body, script_name: (row.scripts || [])[0] || "", script_source: "", enabled: row.enabled, scripts: row.scripts || [], sources: {}, storage_path: "" }
    : empty();
  mode.value = next;
  open.value = true;
  if (row) loadScripts(row.id);
}
async function loadScripts(id) {
  try {
    const detail = await api(`/api/skills/${id}`);
    if (form.value.id !== id) return;
    const sources = detail.script_sources || {};
    const scripts = detail.scripts || [];
    const scriptName = scripts[0] || "";
    form.value = { ...form.value, scripts, sources, script_name: scriptName, script_source: sources[scriptName] || "", storage_path: detail.storage_path || "" };
  } catch (err) { desk.error.value = err.message; }
}
function rememberSource() {
  if (!form.value.script_name) return;
  form.value.sources = { ...form.value.sources, [form.value.script_name]: form.value.script_source };
}
function pickScript() {
  form.value.script_source = form.value.sources[form.value.script_name] || "";
}
async function save() {
  try {
    rememberSource();
    const body = {
      name: form.value.name,
      description: form.value.description,
      body: form.value.body,
      enabled: form.value.enabled,
    };
    if (mode.value === "create") {
      body.script_name = form.value.script_name;
      body.script_source = form.value.script_source;
    } else if (Object.keys(form.value.sources || {}).length) {
      body.script_files = form.value.sources;
    }
    if (mode.value === "create") await api("/api/skills", { method: "POST", body: { ...body, key: form.value.key } });
    else await api(`/api/skills/${form.value.id}`, { method: "PATCH", body });
    open.value = false;
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
async function remove(row) {
  if (!window.confirm(`删除技能 ${row.name}？`)) return;
  try { await api(`/api/skills/${row.id}`, { method: "DELETE" }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
async function bulk() {
  if (!window.confirm(`删除所选 ${desk.selected.value.length} 条？`)) return;
  try { await api("/api/skills/bulk-delete", { method: "POST", body: { ids: desk.selected.value } }); await desk.refresh(); } catch (err) { desk.error.value = err.message; }
}
function pickZip() {
  zipInput.value?.click();
}
async function importZip(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    await upload("/api/skills/import", body);
    await desk.refresh();
  } catch (err) { desk.error.value = err.message; }
}
onMounted(() => desk.refresh());
watch(() => route.query.usable_space, () => desk.refresh());
</script>

<template>
  <Desk kicker="技能" title="技能" :sub="`说明交给模型。可上传含 SKILL.md 的压缩包，脚本只在启动时的隔离容器里运行。${departmentNote(route)}`" :columns="columns" :rows="desk.pageData.value.items" :total="desk.pageData.value.total" :query="desk.query" :exact-fields="[{ value: 'name', label: '名称' }, { value: 'key', label: '标识' }]" :error="desk.error.value" :selected="desk.selected.value" :row-mutable="(row) => row.owned !== false" @update:selected="desk.selected.value = $event" @load="desk.refresh()" @create="openWith(null, 'create')" @view="openWith($event, 'view')" @edit="openWith($event, 'edit')" @remove="remove" @bulk="bulk">
    <template #actions>
      <button class="ghost" type="button" @click="pickZip">上传压缩包</button>
      <input ref="zipInput" class="file-input" type="file" accept=".zip,application/zip" aria-label="技能压缩包" @change="importZip" />
    </template>
  </Desk>
  <Dialog :open="open" :mode="mode" :title="form.name || '技能'" @close="open = false" @submit="save">
    <p v-if="mode !== 'create' && form.storage_path" class="field storage-path"><span>存储位置</span><code>{{ form.storage_path }}</code></p>
    <label class="field"><span>名称</span><input v-model="form.name" /></label>
    <label class="field"><span>标识</span><input v-model="form.key" :disabled="mode !== 'create'" placeholder="amount-check" /></label>
    <label class="field"><span>何时使用</span><textarea v-model="form.description" rows="2" placeholder="模型在什么情况下应读取这个技能" /></label>
    <label class="field"><span>说明</span><textarea v-model="form.body" rows="8" placeholder="步骤、约束和输出格式" /></label>
    <label v-if="mode === 'create'" class="field"><span>脚本文件</span><input v-model="form.script_name" placeholder="run.py" /></label>
    <label v-else class="field"><span>脚本文件</span>
      <select v-model="form.script_name" aria-label="脚本文件" @change="pickScript">
        <option v-if="!form.scripts.length" value="">没有脚本</option>
        <option v-for="name in form.scripts" :key="name" :value="name">{{ name }}</option>
      </select>
    </label>
    <label class="field"><span>脚本内容</span><textarea v-model="form.script_source" rows="8" placeholder="支持 .py、.js、.mjs、.java。留空则不改已有脚本。" @input="rememberSource" /></label>
    <label class="check"><input type="checkbox" v-model="form.enabled" /> 启用。停用后不能挂到智能体。</label>
  </Dialog>
</template>
