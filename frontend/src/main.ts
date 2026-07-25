import { createApp } from "vue";
import { createPinia } from "pinia";
import { createRouter, createWebHistory } from "vue-router";
import App from "@/App.vue";
import RunsView from "@/views/RunsView.vue";
import CreateRunView from "@/views/CreateRunView.vue";
import RunDetailView from "@/views/RunDetailView.vue";
import SettingsView from "@/views/SettingsView.vue";
import "@/styles/main.css";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/runs" },
    { path: "/runs", component: RunsView, meta: { title: "运行任务" } },
    { path: "/runs/new", component: CreateRunView, meta: { title: "创建运行" } },
    {
      path: "/runs/:runId",
      component: RunDetailView,
      props: true,
      meta: { title: "运行详情" },
    },
    { path: "/settings", component: SettingsView, meta: { title: "系统设置" } },
  ],
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? "控制台")} · MegaDeepagents`;
});

createApp(App).use(createPinia()).use(router).mount("#app");
