import { reactive, ref } from "vue";
import { api } from "./api";

export function useDesk(path) {
  const query = reactive({
    page: 1,
    page_size: 20,
    q: "",
    exact_field: "",
    exact: "",
    time_from: "",
    time_to: "",
  });
  const pageData = ref({ items: [], total: 0, page: 1, page_size: 20 });
  const error = ref("");
  const selected = ref([]);

  async function load() {
    const params = new URLSearchParams();
    Object.entries(query).forEach(([key, value]) => {
      if (value !== "" && value !== null && value !== undefined) params.set(key, String(value));
    });
    const target = typeof path === "function" ? path() : path;
    if (!target) return;
    const joiner = target.includes("?") ? "&" : "?";
    const data = await api(`${target}${joiner}${params.toString()}`);
    pageData.value = Array.isArray(data)
      ? { items: data, total: data.length, page: 1, page_size: data.length || query.page_size }
      : data;
    selected.value = [];
  }

  async function refresh() {
    try {
      error.value = "";
      await load();
    } catch (err) {
      error.value = err.message;
    }
  }

  return { query, pageData, error, selected, refresh };
}

export function blank(source) {
  return JSON.parse(JSON.stringify(source));
}

export function withScope(route, path, key = "usable_space") {
  const id = route.query[key];
  if (!id) return path;
  return `${path}?${key}=${encodeURIComponent(String(id))}`;
}

export function departmentNote(route) {
  const name = route.query.department;
  return name ? `只显示「${name}」的对应记录。` : "";
}

export function when(value) {
  if (!value) return "—";
  return String(value).replace("T", " ").slice(0, 16);
}
