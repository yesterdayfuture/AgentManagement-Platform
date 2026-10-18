let expiryTimer = 0;
let unauthorized = () => {
  if (window.location.pathname !== "/login") window.location.assign("/login");
};

/** 由 main.js 接上路由，避免接口层和路由互相引用。 */
export function onUnauthorized(handler) {
  unauthorized = handler;
}

export function tokenExpired(token = localStorage.getItem("token") || "") {
  const raw = String(token).split(".")[0];
  if (!raw) return true;
  try {
    const padded = raw.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (raw.length % 4)) % 4);
    const data = JSON.parse(atob(padded));
    return Number(data.exp || 0) * 1000 <= Date.now();
  } catch {
    return true;
  }
}

export function expireSession() {
  window.clearTimeout(expiryTimer);
  clearSession();
  unauthorized();
}

export function watchExpiry() {
  window.clearTimeout(expiryTimer);
  const token = localStorage.getItem("token");
  if (!token) return;
  const raw = token.split(".")[0];
  let exp = 0;
  try {
    const padded = raw.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (raw.length % 4)) % 4);
    exp = Number(JSON.parse(atob(padded)).exp || 0);
  } catch {
    expireSession();
    return;
  }
  const wait = exp * 1000 - Date.now();
  if (wait <= 0) {
    expireSession();
    return;
  }
  expiryTimer = window.setTimeout(expireSession, wait);
}

function reject(response, data, path) {
  const detail = data.detail;
  const message = typeof detail === "string" ? detail : detail?.message || "请求失败";
  if (response.status === 401 && !String(path).startsWith("/api/auth/")) expireSession();
  const error = new Error(message);
  error.status = response.status;
  error.payload = data;
  return error;
}

export async function upload(path, form) {
  const headers = {};
  const token = localStorage.getItem("token");
  const spaceId = localStorage.getItem("spaceId");
  if (token) headers.Authorization = `Bearer ${token}`;
  if (spaceId) headers["X-Space-Id"] = spaceId;
  const response = await fetch(path, { method: "POST", headers, body: form });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw reject(response, data, path);
  return data;
}

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const token = localStorage.getItem("token");
  const spaceId = localStorage.getItem("spaceId");
  if (token) headers.Authorization = `Bearer ${token}`;
  if (spaceId && !headers["X-Space-Id"]) headers["X-Space-Id"] = spaceId;
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw reject(response, data, path);
  return data;
}

export async function stream(path, body, onEvent) {
  const headers = { "Content-Type": "application/json" };
  const token = localStorage.getItem("token");
  const spaceId = localStorage.getItem("spaceId");
  if (token) headers.Authorization = `Bearer ${token}`;
  if (spaceId) headers["X-Space-Id"] = spaceId;
  const response = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw reject(response, data, path);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const line = part.split("\n").find((item) => item.startsWith("data:"));
      if (!line) continue;
      onEvent(JSON.parse(line.slice(5).trim()));
    }
  }
}

export function session() {
  return {
    token: localStorage.getItem("token") || "",
    spaceId: localStorage.getItem("spaceId") || "",
    user: JSON.parse(localStorage.getItem("user") || "null"),
    spaces: JSON.parse(localStorage.getItem("spaces") || "[]"),
  };
}

export function saveSession({ token, user, spaces, spaceId }) {
  localStorage.setItem("token", token);
  localStorage.setItem("user", JSON.stringify(user));
  localStorage.setItem("spaces", JSON.stringify(spaces));
  localStorage.setItem("spaceId", String(spaceId));
  watchExpiry();
}

export function clearSession() {
  localStorage.removeItem("token");
  localStorage.removeItem("user");
  localStorage.removeItem("spaces");
  localStorage.removeItem("spaceId");
}
