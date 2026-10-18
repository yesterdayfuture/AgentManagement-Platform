<script setup>
import { computed } from "vue";
import { countText } from "../format";
const props = defineProps({ items: { type: Array, default: () => [] }, label: String, unit: { type: String, default: "次" } });
const colors = ["#2864e8", "#159a9c", "#8c7ccd", "#d39425", "#cc4054", "#73839b"];
const total = computed(() => props.items.reduce((sum, item) => sum + Math.max(0, Number(item.value) || 0), 0));
const segments = computed(() => {
  let offset = 0;
  return props.items.map((item, index) => {
    const share = total.value ? Math.max(0, Number(item.value) || 0) / total.value * 100 : 0;
    const segment = { ...item, share, offset, color: item.color || colors[index % colors.length] };
    offset += share;
    return segment;
  });
});
</script>
<template>
  <div class="distribution-chart">
    <div class="donut-wrap">
      <svg viewBox="0 0 120 120" role="img" :aria-label="`${label}，共 ${total} ${unit}`">
        <circle class="donut-track" cx="60" cy="60" r="46" />
        <circle v-for="item in segments" :key="item.label" cx="60" cy="60" r="46" pathLength="100" fill="none" :stroke="item.color" stroke-width="12" :stroke-dasharray="`${item.share} ${100 - item.share}`" :stroke-dashoffset="-item.offset" transform="rotate(-90 60 60)"><title>{{ item.label }}：{{ item.value }} {{ unit }}（{{ item.share.toFixed(1) }}%）</title></circle>
      </svg>
      <div class="donut-center"><strong>{{ countText(total) }}</strong><span>{{ unit }}</span></div>
    </div>
    <ul class="chart-legend" v-if="total">
      <li v-for="item in segments" :key="item.label"><i :style="{ background: item.color }"></i><span :title="item.label">{{ item.label }}</span><b :title="String(item.value)">{{ countText(item.value) }}</b><small>{{ item.share.toFixed(1) }}%</small></li>
    </ul>
    <p v-else class="sub">当前范围暂无数据</p>
  </div>
</template>
