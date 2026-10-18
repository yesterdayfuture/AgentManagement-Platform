<script setup>
import { computed } from "vue";
const props = defineProps({ value: [String, Boolean, Number], label: [String, Boolean, Number] });
const tone = computed(() => {
  if ([true, 'approved', 'completed', 'success', 'succeeded', 'online', 'active', 'low'].includes(props.value)) return 'success';
  if (['failed', 'error', 'rejected', 'high', 'denied'].includes(props.value)) return 'danger';
  if (['pending', 'paused', 'waiting_approval', 'medium'].includes(props.value)) return 'warning';
  if (['running', 'queued'].includes(props.value)) return 'info';
  return 'neutral';
});
</script>
<template><span class="status-badge" :class="tone">{{ label ?? value }}</span></template>
