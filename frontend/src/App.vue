<script setup lang="ts">
import {
  Activity,
  CloudSun,
  ListTree,
  Plus,
  Settings,
  Sparkles,
} from "@lucide/vue";
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <RouterLink class="brand" to="/runs">
        <span class="brand-mark"><Sparkles :size="20" /></span>
        <span>
          <strong>MegaDeepagents</strong>
          <small>Agent Control Plane</small>
        </span>
      </RouterLink>

      <nav class="primary-nav" aria-label="主导航">
        <span class="nav-label">Workspace</span>
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
