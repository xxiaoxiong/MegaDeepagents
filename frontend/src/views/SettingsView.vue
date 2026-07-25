<script setup lang="ts">
import { onMounted, ref } from "vue";
import { CheckCircle2, Database, KeyRound, RefreshCw, Save, ServerCog } from "@lucide/vue";
import { api } from "@/lib/api";

const apiBase = ref(localStorage.getItem("megadeepagents_api_base") ?? "");
const server = ref<Record<string, unknown>>({});
const loading = ref(false);
const saved = ref(false);
const error = ref("");

async function load() {
  loading.value = true;
  error.value = "";
  try {
    server.value = await api.settings();
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    loading.value = false;
  }
}

function save() {
  const value = apiBase.value.trim().replace(/\/$/, "");
  if (value) localStorage.setItem("megadeepagents_api_base", value);
  else localStorage.removeItem("megadeepagents_api_base");
  saved.value = true;
  window.setTimeout(() => (saved.value = false), 1800);
  load();
}

onMounted(load);
</script>

<template>
  <div class="page settings-page">
    <header class="page-header">
      <div>
        <span class="eyebrow">Configuration</span>
        <h1>系统设置</h1>
        <p>浏览器只保存 API 地址；模型密钥与运行时策略始终由后端安全管理。</p>
      </div>
      <button class="btn btn-secondary" :disabled="loading" @click="load">
        <RefreshCw :class="{ spin: loading }" :size="15" /> 检查连接
      </button>
    </header>

    <div v-if="error" class="notice error">{{ error }}</div>
    <div class="settings-grid">
      <section class="panel setting-card">
        <span class="setting-icon"><ServerCog :size="20" /></span>
        <div>
          <span class="eyebrow">Browser connection</span>
          <h2>后端 API 地址</h2>
          <p>同源部署时留空。本地独立启动或 Vercel 部署时填写后端的 HTTPS 地址。</p>
          <label class="field">
            <span>API Base URL</span>
            <input v-model="apiBase" type="url" placeholder="https://api.example.com" />
          </label>
          <button class="btn btn-primary" @click="save">
            <CheckCircle2 v-if="saved" :size="15" />
            <Save v-else :size="15" />
            {{ saved ? "已保存" : "保存并连接" }}
          </button>
        </div>
      </section>

      <section class="panel setting-card">
        <span class="setting-icon"><KeyRound :size="20" /></span>
        <div>
          <span class="eyebrow">Model runtime</span>
          <h2>模型与可观测性</h2>
          <dl class="settings-list">
            <div><dt>Provider</dt><dd>{{ server.llm_provider ?? "—" }}</dd></div>
            <div><dt>Model</dt><dd>{{ server.llm_model ?? "—" }}</dd></div>
            <div><dt>API Key</dt><dd>{{ server.llm_api_key_configured ? "已配置" : "未配置" }}</dd></div>
            <div><dt>LangSmith</dt><dd>{{ server.langsmith_enabled ? "已启用" : "离线模式" }}</dd></div>
          </dl>
          <p class="security-note">API 不返回任何密钥明文。请通过部署平台环境变量完成配置。</p>
        </div>
      </section>

      <section class="panel setting-card">
        <span class="setting-icon"><Database :size="20" /></span>
        <div>
          <span class="eyebrow">Control plane</span>
          <h2>运行时限制</h2>
          <dl class="settings-list">
            <div><dt>环境</dt><dd>{{ server.app_env ?? "—" }}</dd></div>
            <div><dt>最大并发</dt><dd>{{ server.max_concurrency ?? "—" }}</dd></div>
            <div><dt>最大团队</dt><dd>{{ server.max_team_size ?? "—" }}</dd></div>
            <div><dt>低风险自动审批</dt><dd>{{ server.default_auto_approve_low_risk ? "开启" : "关闭" }}</dd></div>
          </dl>
        </div>
      </section>
    </div>
  </div>
</template>
