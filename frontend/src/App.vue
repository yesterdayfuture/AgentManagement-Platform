<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import Icon from "./components/Icon.vue";
import Dialog from "./components/Dialog.vue";
import { api, clearSession, expireSession, saveSession, session, tokenExpired, watchExpiry } from "./api";

const router = useRouter();
const route = useRoute();
const navSource = [
  { label: "工作台", items: [["overview", "运行总览", "/overview"]] },
  { label: "资源管理", items: [["agents", "智能体", "/agents"], ["skills", "技能", "/skills"], ["tools", "工具服务", "/tools"], ["models", "模型管理", "/models"], ["knowledge", "知识库", "/knowledge"], ["memories", "记忆管理", "/memories"]] },
  { label: "编排与观测", items: [["ask", "模型问答", "/ask"], ["workflows", "工作流", "/workflows"], ["runs", "执行记录", "/workflow-runs"], ["traces", "调用轨迹", "/traces"], ["evals", "评测中心", "/evals"], ["traces", "质量评价", "/quality-reviews"]] },
  { label: "安全与治理", items: [["departments", "部门管理", "/departments"], ["approvals", "审批中心", "/approvals"], ["sensitive", "敏感词", "/sensitive-words"], ["governance", "空间治理", "/governance"]] },
];
const isDeptAdmin = computed(() => state.value.spaces?.some((item) => item.role === "admin"));
const navGroups = computed(() => navSource.map((group) => ({
  ...group,
  items: group.items.filter((item) => item[2] !== "/quality-reviews" && (!["/departments", "/governance"].includes(item[2]) || isPlatform.value || isDeptAdmin.value) || item[2] === "/quality-reviews" && isPlatform.value),
})));
const activeNav = computed(() => navGroups.value.flatMap(group => group.items).find(item => route.path === item[2] || route.path.startsWith(item[2] + "/")));
const state = ref(session());
const current = ref(null);
const passwordOpen = ref(false);
const passwordForm = ref({ old_password: "", new_password: "", confirm: "" });
const passwordError = ref("");
const isPlatform = computed(() => Boolean(state.value.user?.platform_admin));
const roleLabel = computed(() => {
  if (isPlatform.value) return "平台管理员";
  const labels = { admin: "部门管理员", builder: "构建者", operator: "操作者", viewer: "只读" };
  return labels[current.value?.role] || current.value?.role || "";
});

async function refresh() {
  state.value = session();
  if (!state.value.token || route.meta.public) return;
  try {
    const me = await api("/api/me");
    const saved = session();
    const spaceId = me.platform_admin ? me.spaces[0]?.id : (me.spaces.find((item) => item.role === "admin")?.id || me.spaces[0]?.id);
    saveSession({
      token: saved.token,
      user: { ...saved.user, username: me.username, display_name: me.display_name, platform_admin: me.platform_admin },
      spaces: me.spaces,
      spaceId: String(spaceId || ""),
    });
    state.value = session();
    current.value = await api("/api/spaces/current");
  } catch {
    current.value = null;
  }
}

function guardSession() {
  if (route.meta.public) return;
  if (!localStorage.getItem("token") || tokenExpired()) expireSession();
}
onMounted(() => {
  watchExpiry();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") guardSession();
  });
  refresh();
});
watch(() => route.fullPath, refresh);

async function changePassword() {
  passwordError.value = "";
  if (passwordForm.value.new_password !== passwordForm.value.confirm) {
    passwordError.value = "两次输入的新密码不一致";
    return;
  }
  try {
    await api("/api/me/password", {
      method: "POST",
      body: { old_password: passwordForm.value.old_password, new_password: passwordForm.value.new_password },
    });
    passwordOpen.value = false;
    passwordForm.value = { old_password: "", new_password: "", confirm: "" };
  } catch (err) {
    passwordError.value = err.message;
  }
}
function logout() {
  clearSession();
  current.value = null;
  router.push("/login");
}
</script>

<template>
  <router-view v-if="route.meta.public" />
  <div v-else class="shell">
    <aside class="rail">
      <router-link class="mark" to="/overview" aria-label="LIMEN 运行总览">
        <b class="brand-symbol">垣</b>
        <span><strong>LIMEN</strong><small>智能体管理平台</small></span>
      </router-link>
      <nav class="nav" aria-label="主导航">
        <div v-for="group in navGroups" :key="group.label" class="nav-group">
          <p class="nav-label">{{ group.label }}</p>
          <router-link v-for="item in group.items" :key="item[2]" :to="item[2]">
            <Icon :name="item[0]" /><span>{{ item[1] }}</span>
          </router-link>
        </div>
      </nav>
      <footer><span class="rail-foot-mark">L</span><div>Agent control plane<small>构建 · 编排 · 观测</small></div></footer>
    </aside>
    <section class="main">
      <header class="workspace-bar">
        <div class="breadcrumb"><span>控制台</span><span class="breadcrumb-separator">/</span><strong>{{ activeNav?.[1] || "详情" }}</strong></div>
        <div class="workspace-controls">
          <span class="workspace-scope" :title="isPlatform ? '查看所有空间的数据' : state.spaces.map(item => item.name).join('、')">{{ isPlatform ? '全部空间' : `所属空间：${state.spaces.map(item => item.name).join('、')}` }}</span>
          <span class="role-badge" v-if="roleLabel">{{ roleLabel }}</span>
          <button class="ghost" type="button" @click="passwordOpen = true">修改密码</button>
          <button class="ghost logout" @click="logout" aria-label="退出登录" title="退出登录"><Icon name="logout" /></button>
        </div>
      </header>
      <main class="page-content">
      <router-view :key="state.spaceId" />
      <Dialog :open="passwordOpen" mode="edit" title="修改密码" @close="passwordOpen = false" @submit="changePassword">
        <label class="field"><span>原密码</span><input v-model="passwordForm.old_password" type="password" autocomplete="current-password" /></label>
        <label class="field"><span>新密码</span><input v-model="passwordForm.new_password" type="password" autocomplete="new-password" /></label>
        <label class="field"><span>再输入一次</span><input v-model="passwordForm.confirm" type="password" autocomplete="new-password" /></label>
        <p class="sub">至少 6 位。只修改当前登录账号。</p>
        <p v-if="passwordError" class="error">{{ passwordError }}</p>
      </Dialog>
      </main>
    </section>
  </div>
</template>
