<script setup>
defineProps({
  open: Boolean,
  title: String,
  mode: { type: String, default: "view" },
  wide: Boolean,
  live: Boolean,
});
const emit = defineEmits(["close", "submit"]);
const labels = { create: "新增", edit: "编辑", view: "查看", run: "执行" };
</script>

<template>
  <div class="veil" v-if="open" @click.self="emit('close')">
    <div class="dialog" :class="{ wide }" role="dialog" aria-modal="true" :aria-label="title">
      <button class="dialog-x" type="button" aria-label="关闭" @click="emit('close')">×</button>
      <p class="kicker">{{ labels[mode] || "记录" }}</p>
      <h2>{{ title }}</h2>
      <fieldset :disabled="mode === 'view' && !live">
        <slot />
      </fieldset>
      <div class="dialog-actions" v-if="mode === 'create' || mode === 'edit'">
        <button class="btn" type="button" @click="emit('submit')">{{ mode === "create" ? "保存" : "更新" }}</button>
        <slot name="actions" />
      </div>
      <slot name="footer" />
    </div>
  </div>
</template>
