<script setup>
import { computed } from "vue";
import StatusBadge from "./StatusBadge.vue";

const props = defineProps({
  kicker: String,
  title: String,
  sub: String,
  actionsWidth: { type: Number, default: 300 },
  columns: { type: Array, default: () => [] },
  rows: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  query: { type: Object, required: true },
  exactFields: { type: Array, default: () => [] },
  error: { type: String, default: "" },
  selected: { type: Array, default: () => [] },
  creatable: { type: Boolean, default: true },
  editable: { type: Boolean, default: true },
  removable: { type: Boolean, default: true },
  rowMutable: { type: Function, default: null },
  createLabel: { type: String, default: "新增" },
  viewLabel: { type: String, default: "查看" },
  emptyText: { type: String, default: "还没有记录。" },
});
const emit = defineEmits(["load", "reset", "create", "view", "edit", "remove", "bulk", "update:selected"]);
const pageCount = computed(() => Math.max(1, Math.ceil((props.total || 0) / Number(props.query.page_size || 20))));
const allChecked = computed(() => props.rows.length > 0 && props.rows.every((row) => props.selected.includes(row.id)));

function search() {
  props.query.page = 1;
  emit("load");
}
function reset() {
  props.query.q = "";
  props.query.exact_field = "";
  props.query.exact = "";
  props.query.time_from = "";
  props.query.time_to = "";
  props.query.page = 1;
  props.query.page_size = 20;
  emit("reset");
  emit("load");
}
function toggleAll(event) {
  emit("update:selected", event.target.checked ? props.rows.map((row) => row.id) : []);
}
function toggle(id, event) {
  const next = new Set(props.selected);
  if (event.target.checked) next.add(id);
  else next.delete(id);
  emit("update:selected", [...next]);
}
function cell(row, column) {
  const value = row[column.key];
  if (column.format) return column.format(value, row);
  if (value === null || value === undefined || value === "") return "—";
  return value;
}
</script>

<template>
  <p class="kicker">{{ kicker?.replace(/^\d+\s*/, "") }}</p>
  <div class="top">
    <div>
      <h1>{{ title }}</h1>
      <p class="sub">{{ sub }}</p>
    </div>
    <slot name="actions" />
    <button v-if="creatable" class="btn" type="button" @click="emit('create')">{{ createLabel }}</button>
  </div>
  <p class="error">{{ error }}</p>
  <div class="row filters">
    <label>关键词<input v-model="query.q" placeholder="关键字" @keydown.enter="search" /></label>
    <label>匹配字段
      <select v-model="query.exact_field">
        <option value="">不限</option>
        <option v-for="field in exactFields" :key="field.value" :value="field.value">{{ field.label }}</option>
      </select>
    </label>
    <label>匹配值<input v-model="query.exact" @keydown.enter="search" /></label>
    <label>开始时间<input v-model="query.time_from" type="datetime-local" /></label>
    <label>结束时间<input v-model="query.time_to" type="datetime-local" /></label>
    <label>每页<input v-model.number="query.page_size" type="number" min="1" max="100" /></label>
    <button class="ghost" type="button" @click="search">查询</button>
    <button class="ghost" type="button" @click="reset">重置</button>
    <slot name="filters" />
  </div>
  <div class="sheet data-sheet">
    <div class="list-heading">
      <strong>{{ title || "数据列表" }}</strong>
      <span class="count-badge">{{ total }} 条记录</span>
      <button v-if="removable" class="ghost danger bulk-delete" type="button" :disabled="!selected.length" :title="selected.length ? `删除已勾选的 ${selected.length} 条` : '先勾选要删除的记录'" @click="emit('bulk')">批量删除{{ selected.length ? `（${selected.length}）` : "" }}</button>
    </div>
    <div class="table-scroll" tabindex="0" role="region" :aria-label="`${title || '数据'}列表`">
    <table :style="{ minWidth: `${(removable ? 36 : 0) + columns.length * 140 + actionsWidth}px` }">
      <colgroup>
        <col v-if="removable" style="width: 36px" />
        <col v-for="column in columns" :key="column.key" />
        <col :style="{ width: `${actionsWidth}px` }" />
      </colgroup>
      <thead>
        <tr>
          <th v-if="removable" class="check-col"><input type="checkbox" aria-label="全选本页" :checked="allChecked" @change="toggleAll" /></th>
          <th v-for="column in columns" :key="column.key">{{ column.label }}</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="!rows.length"><td class="empty-cell" :colspan="columns.length + 1 + (removable ? 1 : 0)">{{ emptyText }}</td></tr>
        <tr v-for="row in rows" :key="row.id" :class="{ selected: selected.includes(row.id) }">
          <td v-if="removable"><input type="checkbox" :aria-label="`选择 ${row.id}`" :checked="selected.includes(row.id)" @change="toggle(row.id, $event)" /></td>
          <td v-for="column in columns" :key="column.key" :title="column.format ? String(row[column.key] ?? '') : String(cell(row, column))">
            <slot :name="`cell-${column.key}`" :row="row" :value="cell(row, column)">
              <StatusBadge v-if="['status', 'review_status', 'risk', 'enabled'].includes(column.key)" :value="row[column.key]" :label="cell(row, column)" />
              <template v-else>{{ cell(row, column) }}</template>
            </slot>
          </td>
          <td><div class="actions">
            <slot name="extra" :row="row" />
            <button class="ghost" type="button" @click="emit('view', row)">{{ viewLabel }}</button>
            <button v-if="editable && (!rowMutable || rowMutable(row))" class="ghost" type="button" @click="emit('edit', row)">编辑</button>
            <button v-if="removable && (!rowMutable || rowMutable(row))" class="ghost danger" type="button" @click="emit('remove', row)">删除</button>
          </div></td>
        </tr>
      </tbody>
    </table>
    </div>
    <div class="pager">
      <span>共 {{ total }} 条 · 已选 {{ selected.length }}</span>
      <div class="row">
        <button v-if="removable" class="ghost danger" type="button" :disabled="!selected.length" @click="emit('bulk')">批量删除</button>
        <button class="ghost" type="button" :disabled="query.page <= 1" @click="query.page -= 1; emit('load')">上一页</button>
        <span>{{ query.page }} / {{ pageCount }}</span>
        <button class="ghost" type="button" :disabled="query.page >= pageCount" @click="query.page += 1; emit('load')">下一页</button>
      </div>
    </div>
  </div>
</template>
