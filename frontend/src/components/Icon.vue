<script setup>
defineProps({ name: String });
const paths = {
  overview: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  agents: 'M8 4h8 M12 2v2 M5 7h14v13H5z M9 11v2 M15 11v2 M9 17h6 M2 11v5 M22 11v5',
  tools: 'm14 3-4 4 7 7 4-4 M3 21l9-9 M3 3l18 18 M3 3l1 5 4-4z',
  models: 'm12 2 10 5-10 5L2 7z M2 12l10 5 10-5 M2 17l10 5 10-5',
  knowledge: 'M4 3h7v18H4z M14 4l6-1 3 17-6 1z M6 7h3 M6 16h3',
  workflows: 'M3 3h6v6H3z M15 15h6v6h-6z M15 3h6v6h-6z M9 6h6 M6 9v9h9',
  runs: 'm9 7 8 5-8 5z M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2',
  approvals: 'm12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6z m-5 10 3 3 7-7',
  traces: 'M3 3v18h18 M6 14l4-5 4 7 7-10',
  evals: 'M8 3h8v4H8z M8 5H4v17h16V5h-4 M8 12l2 2 5-4 M8 18h8',
  memories: 'M8 3H5v18h3 M16 3h3v18h-3 M9 8h6v8H9z M12 5v3 M12 16v3',
  skills: 'M4 4h10v16H4z M14 8h6v12h-6 M7 8h4 M7 12h4 M7 16h3',
  governance: 'M3 7h18 M3 17h18 M8 4v6 M16 14v6',
  departments: 'M4 20V9l8-5 8 5v11 M9 20v-6h6v6',
  logout: 'M9 3H3v18h6 M10 12h11 m-4-4 4 4-4 4',
  ask: 'M4 5h12a3 3 0 0 1 3 3v5a3 3 0 0 1-3 3H9l-4 3v-3H4a3 3 0 0 1-3-3V8a3 3 0 0 1 3-3z',
};
</script>
<template><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="paths[name] || paths.overview" /></svg></template>
