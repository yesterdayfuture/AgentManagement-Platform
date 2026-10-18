<script setup>
import { ref } from "vue";
import { useRouter } from "vue-router";
import { api, saveSession } from "../api";

const router = useRouter();
const username = ref("builder");
const password = ref("builder123");
const error = ref("");

async function submit() {
  error.value = "";
  try {
    const login = await api("/api/auth/login", { method: "POST", body: { username: username.value, password: password.value } });
    const me = await api("/api/me", { headers: { Authorization: `Bearer ${login.access_token}` } });
    saveSession({ token: login.access_token, user: me, spaces: me.spaces, spaceId: me.spaces[0].id });
    router.push("/overview");
  } catch (err) {
    error.value = err.message;
  }
}
</script>

<template>
  <div class="login">
    <section class="login-hero">
      <p class="kicker">AGENT CONTROL PLANE</p>
      <div class="login-brand">垣 <span>LIMEN</span></div>
      <h1>让每一次智能调用<br />清晰可控。</h1>
      <p class="sub">连接模型与工具，编排智能体工作流，在同一工作空间追踪执行、评估质量并管理权限。</p>
      <div class="control-diagram" aria-hidden="true"><div>模型与知识</div><i></i><div class="diagram-core">LIMEN<br /><small>智能体运行控制</small></div><i></i><div>工作流与工具</div></div>
      <p class="login-caption">AGENTS / WORKFLOWS / OBSERVABILITY</p>
    </section>
    <form class="login-panel" @submit.prevent="submit">
      <p class="kicker">进入空间</p>
      <h2 style="margin: 8px 0 24px">登录工作空间</h2>
      <label class="field"><span>账号</span><input v-model="username" autocomplete="username" /></label>
      <label class="field"><span>密码</span><input v-model="password" type="password" autocomplete="current-password" /></label>
      <p class="error">{{ error }}</p>
      <button class="btn" type="submit">登录控制台 →</button>
      <p class="sub">builder / builder123 · operator / operator123 · admin / admin123</p>
    </form>
  </div>
</template>
