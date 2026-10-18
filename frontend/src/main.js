import { createApp } from "vue";
import App from "./App.vue";
import { onUnauthorized } from "./api";
import router from "./router";
import "./styles.css";

onUnauthorized(() => {
  if (router.currentRoute.value.path !== "/login") router.replace("/login");
});

createApp(App).use(router).mount("#app");
