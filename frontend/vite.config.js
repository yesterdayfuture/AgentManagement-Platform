import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            const type = proxyRes.headers["content-type"] || "";
            if (type.includes("text/event-stream")) {
              proxyRes.headers["cache-control"] = "no-cache, no-transform";
              proxyRes.headers["x-accel-buffering"] = "no";
            }
          });
        },
      },
      "/v1": "http://127.0.0.1:8000",
    },
  },
});
