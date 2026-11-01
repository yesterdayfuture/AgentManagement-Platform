<script setup>
import { Comment, Fragment, computed, nextTick, onBeforeUnmount, onMounted, ref, useSlots } from 'vue';
const props = defineProps({ modelValue: { default: undefined }, value: { default: undefined }, disabled: Boolean });
const emit = defineEmits(['update:modelValue', 'change']);
const slots = useSlots();
const root = ref(null);
const input = ref(null);
const open = ref(false);
const query = ref('');
const active = ref(0);
function labelOf(children) {
  if (typeof children === 'string' || typeof children === 'number') return String(children);
  if (Array.isArray(children)) return children.map((item) => labelOf(item?.children)).join('');
  return '';
}
function collect(nodes, result = []) {
  for (const node of nodes || []) {
    if (!node || node.type === Comment) continue;
    if (node.type === Fragment) { collect(node.children, result); continue; }
    if (node.type === 'option') {
      const label = labelOf(node.children).trim();
      result.push({ label, value: Object.prototype.hasOwnProperty.call(node.props || {}, 'value') ? node.props.value : label, disabled: Boolean(node.props?.disabled) });
    } else if (Array.isArray(node.children)) collect(node.children, result);
  }
  return result;
}
const options = computed(() => collect(slots.default?.()));
const selectedValue = computed(() => props.modelValue !== undefined ? props.modelValue : props.value);
const selected = computed(() => options.value.find((item) => String(item.value) === String(selectedValue.value)));
const filtered = computed(() => options.value.filter((item) => item.label.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())));
async function toggle() {
  if (props.disabled) return;
  open.value = !open.value;
  query.value = '';
  active.value = Math.max(0, filtered.value.findIndex((item) => String(item.value) === String(selectedValue.value)));
  if (open.value) { await nextTick(); input.value?.focus(); }
}
function focusInput() {
  if (props.disabled || open.value) return;
  open.value = true;
  query.value = '';
  active.value = 0;
}
function typeInput(event) { open.value = true; query.value = event.target.value; active.value = 0; }
function choose(item) {
  if (item.disabled || !open.value) return;
  emit('update:modelValue', item.value);
  emit('change', { target: { value: item.value == null ? '' : String(item.value) } });
  open.value = false;
}
function onKey(event) {
  if (event.key === 'Escape') { open.value = false; event.preventDefault(); }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); active.value = Math.max(0, Math.min(filtered.value.length - 1, active.value + (event.key === 'ArrowDown' ? 1 : -1))); }
  if (event.key === 'Enter' && filtered.value[active.value]) { event.preventDefault(); choose(filtered.value[active.value]); }
}
function outside(event) { if (root.value && !root.value.contains(event.target)) open.value = false; }
function focusOut() { queueMicrotask(() => { if (root.value && !root.value.contains(document.activeElement)) open.value = false; }); }
onMounted(() => document.addEventListener('pointerdown', outside));
onBeforeUnmount(() => document.removeEventListener('pointerdown', outside));
</script>
<template>
  <div ref="root" class="search-select" :class="{ 'is-open': open, 'is-disabled': disabled }" @keydown="onKey" @focusout="focusOut">
    <div class="search-trigger">
      <input ref="input" type="text" role="combobox" autocomplete="off" :disabled="disabled" :value="open ? query : (selected?.label || '')" :placeholder="selected?.label || '请选择或输入筛选'" :aria-label="$attrs['aria-label'] || '选择或搜索选项'" :aria-expanded="open" aria-autocomplete="list" aria-haspopup="listbox" @focus="focusInput" @input="typeInput" />
      <button type="button" class="chevron" :disabled="disabled" aria-label="展开选项" @mousedown.prevent @click="toggle">⌄</button>
    </div>
    <div v-if="open" class="search-panel">
      <div class="search-list" role="listbox">
        <button v-for="(item, index) in filtered" :key="`${index}-${item.value}`" type="button" role="option" class="search-option" :class="{ active: index === active, selected: String(item.value) === String(selectedValue) }" :aria-selected="String(item.value) === String(selectedValue)" :disabled="item.disabled" @pointerdown.prevent="choose(item)" @mouseenter="active = index" @click="choose(item)">{{ item.label }}</button>
        <p v-if="!filtered.length" class="search-empty">没有匹配选项</p>
      </div>
    </div>
  </div>
</template>
<style scoped>
.search-select { position: relative; display: inline-block; width: 100%; min-width: 0; color: var(--ink); }
.search-trigger { display: flex; align-items: center; width: 100%; height: 38px; border: 1px solid #d6dfeb; border-radius: 7px; background: var(--surface); }
.search-trigger input { flex: 1; width: 1px; min-width: 0; height: 36px; padding: 7px 0 7px 11px; border: 0; box-shadow: none; background: transparent; color: var(--ink); font-size: 13px; text-overflow: ellipsis; }
.search-trigger input:focus, .search-trigger input:focus-visible { border: 0; box-shadow: none; outline: 0; }
.search-trigger:hover, .search-trigger:focus-within, .is-open .search-trigger { border-color: var(--copper); box-shadow: 0 0 0 3px #2864e812; }
.search-trigger .chevron { flex: none; width: 32px; height: 34px; padding: 0; border: 0; background: transparent; color: var(--muted); font-size: 19px; line-height: 1; cursor: pointer; }
.is-disabled { opacity: .55; }
.search-panel { position: absolute; top: calc(100% + 5px); left: 0; z-index: 50; width: max(calc(100% + 72px), 240px); max-width: min(480px, 90vw); padding: 12px; border: 1px solid #d6dfeb; border-radius: 10px; background: var(--surface); box-shadow: 0 14px 34px #172d4b26; }
.search-list { max-height: 260px; overflow-y: auto; padding-top: 5px; }
.search-option { display: block; width: calc(100% - 72px); min-width: 120px; margin-inline: 8px; padding: 8px 12px; border: 0; border-radius: 6px; background: transparent; color: inherit; text-align: left; cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-option:hover, .search-option.active { background: var(--accent-soft); color: var(--copper); }
.search-option.selected { font-weight: 600; }
.search-empty { margin: 0; padding: 10px; color: var(--muted); }
</style>
