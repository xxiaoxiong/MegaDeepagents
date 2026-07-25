<script setup lang="ts">
import {
  Activity,
  Bot,
  ChevronsUp,
  ListTree,
  Plus,
  Settings,
} from "@lucide/vue";
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <RouterLink class="brand" to="/runs">
        <span class="brand-mark"><Bot :size="21" /></span>
        <span>
          <strong>MegaDeepagents</strong>
          <small>Agent Runtime V3</small>
        </span>
      </RouterLink>

      <nav class="primary-nav" aria-label="主导航">
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
        <div class="system-pulse">
          <Activity :size="15" />
          <span>统一运行时</span>
          <i />
        </div>
        <p>LangGraph 编排<br />DeepAgents 执行</p>
      </div>
    </aside>

    <main class="main-surface">
      <header class="mobile-header">
        <Bot :size="20" />
        <strong>MegaDeepagents</strong>
        <ChevronsUp :size="18" />
      </header>
      <RouterView />
    </main>
  </div>
</template>
