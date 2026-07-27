<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import {
  Activity,
  CloudSun,
  ListTree,
  MessageCircle,
  Plus,
  Settings,
  Sparkles,
} from "@lucide/vue";

const route = useRoute();
// 对话视图自带全屏布局（会话侧边栏 + 对话区），跳过全局 shell 侧边栏
const isChatRoute = computed(() => route.path.startsWith("/chat"));
</script>

<template>
  <RouterView v-if="isChatRoute" />

  <div v-else class="app-shell">
    <aside class="sidebar">
      <RouterLink class="brand" to="/chat">
        <span class="brand-mark"><Sparkles :size="20" /></span>
        <span>
          <strong>MegaDeepagents</strong>
          <small>Agent Control Plane</small>
        </span>
      </RouterLink>

      <nav class="primary-nav" aria-label="主导航">
        <span class="nav-label">Workspace</span>
        <RouterLink to="/chat">
          <MessageCircle :size="18" />
          对话
        </RouterLink>
        <RouterLink to="/runs">
          <ListTree :size="18" />
          运行任务
        </RouterLink>
        <RouterLink to="/runs/new">
          <Plus :size="18" />
          创建运行
        </RouterLink>
        <RouterLink to="/settings">
          <Settings :size="18" />
          系统设置
        </RouterLink>
      </nav>

      <div class="sidebar-foot">
        <div class="runtime-card">
          <span class="runtime-orb"><Activity :size="15" /></span>
          <div>
            <strong>统一运行时</strong>
            <small>Root Graph · Healthy</small>
          </div>
          <i />
        </div>
        <div class="system-pulse">
          <CloudSun :size="14" />
          <span>Light workspace</span>
        </div>
        <p>LangGraph 编排 · DeepAgents 执行<br />完整审计 · 可恢复运行</p>
      </div>
    </aside>

    <main class="main-surface">
      <header class="mobile-header">
        <Sparkles :size="19" />
        <strong>MegaDeepagents</strong>
        <CloudSun :size="18" />
      </header>
      <RouterView />
    </main>
  </div>
</template>
