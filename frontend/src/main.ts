import { createApp } from "vue";
import { createPinia } from "pinia";
import { createRouter, createWebHistory } from "vue-router";
import App from "@/App.vue";
import "@/styles/main.css";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/chat" },
    {
      path: "/chat",
      component: () => import("@/views/ChatView.vue"),
      meta: { title: "对话" },
    },
    {
      path: "/chat/:runId",
      component: () => import("@/views/ChatView.vue"),
      props: true,
      meta: { title: "对话" },
    },
    {
      path: "/runs",
      component: () => import("@/views/RunsView.vue"),
      meta: { title: "运行任务" },
    },
    {
      path: "/runs/new",
      component: () => import("@/views/CreateRunView.vue"),
      meta: { title: "创建运行" },
    },
    {
      path: "/runs/:runId",
      component: () => import("@/views/RunDetailView.vue"),
      props: true,
      meta: { title: "运行详情" },
    },
    {
      path: "/settings",
      component: () => import("@/views/SettingsView.vue"),
      meta: { title: "系统设置" },
    },
  ],
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? "控制台")} · MegaDeepagents`;
});

createApp(App).use(createPinia()).use(router).mount("#app");
