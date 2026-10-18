import { createRouter, createWebHistory } from "vue-router";
import { clearSession, tokenExpired } from "./api";
import Login from "./views/Login.vue";
import Overview from "./views/Overview.vue";
import Agents from "./views/Agents.vue";
import AgentStudio from "./views/AgentStudio.vue";
import Tools from "./views/Tools.vue";
import Models from "./views/Models.vue";
import Knowledge from "./views/Knowledge.vue";
import Workflows from "./views/Workflows.vue";
import WorkflowRuns from "./views/WorkflowRuns.vue";
import Approvals from "./views/Approvals.vue";
import Traces from "./views/Traces.vue";
import TraceDetail from "./views/TraceDetail.vue";
import Evals from "./views/Evals.vue";
import Memories from "./views/Memories.vue";
import Skills from "./views/Skills.vue";
import Governance from "./views/Governance.vue";
import Departments from "./views/Departments.vue";
import Ask from "./views/Ask.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", component: Login, meta: { public: true } },
    { path: "/", redirect: "/overview" },
    { path: "/overview", component: Overview },
    { path: "/agents", component: Agents },
    { path: "/agents/:id", component: AgentStudio },
    { path: "/tools", component: Tools },
    { path: "/models", component: Models },
    { path: "/knowledge", component: Knowledge },
    { path: "/workflows", component: Workflows },
    { path: "/workflow-runs", component: WorkflowRuns },
    { path: "/approvals", component: Approvals },
    { path: "/traces", component: Traces },
    { path: "/traces/:id", component: TraceDetail },
    { path: "/evals", component: Evals },
    { path: "/memories", component: Memories },
    { path: "/skills", component: Skills },
    { path: "/governance", component: Governance },
    { path: "/departments", component: Departments },
    { path: "/ask", component: Ask },
  ],
});

router.beforeEach((to) => {
  const token = localStorage.getItem("token");
  if (!to.meta.public && (!token || tokenExpired(token))) {
    clearSession();
    return "/login";
  }
  if (to.path === "/login" && token && !tokenExpired(token)) return "/overview";
  return true;
});

export default router;
