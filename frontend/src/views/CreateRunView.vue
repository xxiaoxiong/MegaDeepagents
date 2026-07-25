<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ArrowLeft, Bot, GitBranch, LoaderCircle, Play, ShieldCheck } from "@lucide/vue";
import { api } from "@/lib/api";
import type { CreateRunInput, RunMode } from "@/types";

const router = useRouter();
const submitting = ref(false);
const error = ref("");
const extraContext = ref("");
const form = reactive({
  goal: "",
  mode: "auto" as RunMode,
  team_template: "software_dev_team",
  repository_path: "",
  base_branch: "main",
  review_required: true,
  auto_approve_low_risk: false,
  max_concurrency: 4,
  max_tasks: 20,
  max_repair_rounds: 3,
});

const canSubmit = computed(() => form.goal.trim().length >= 4 && !submitting.value);

async function submit() {
  if (!canSubmit.value) return;
  submitting.value = true;
  error.value = "";
  const payload: CreateRunInput = {
    goal: form.goal.trim(),
    mode: form.mode,
    team_template: form.team_template,
    repository_path: form.repository_path.trim() || null,
    base_branch: form.repository_path.trim() ? form.base_branch.trim() || null : null,
    review_required: form.review_required,
    auto_approve_low_risk: form.auto_approve_low_risk,
    metadata: {
      max_concurrency: form.max_concurrency,
      max_tasks: form.max_tasks,
      max_repair_rounds: form.max_repair_rounds,
      extra_context: extraContext.value.trim(),
    },
  };
  try {
    const run = await api.createRun(payload);
    await router.push(`/runs/${run.run_id}`);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="page create-page">
    <header class="page-header compact">
      <div>
        <RouterLink class="back-link" to="/runs"><ArrowLeft :size="15" /> 返回运行列表</RouterLink>
        <span class="eyebrow">New run</span>
        <h1>创建一次受治理的 Agent 运行</h1>
        <p>所有模式进入同一个 Root Graph；Auto 会由 Supervisor 基于复杂度选择路径。</p>
      </div>
    </header>

    <form class="create-layout" @submit.prevent="submit">
      <div class="form-main">
        <section class="form-section">
          <div class="section-number">01</div>
          <div class="section-body">
            <h2>任务意图</h2>
            <p>描述预期结果、约束与验收标准，避免只写一个模糊主题。</p>
            <label class="field">
              <span>任务目标</span>
              <textarea
                v-model="form.goal"
                rows="7"
                maxlength="50000"
                placeholder="例如：审计这个仓库的认证模块，修复高风险问题，补齐测试并输出迁移说明。"
                autofocus
              />
              <small>{{ form.goal.length.toLocaleString() }} / 50,000</small>
            </label>
            <label class="field">
              <span>额外上下文</span>
              <textarea
                v-model="extraContext"
                rows="3"
                placeholder="业务背景、禁止修改的目录、上线窗口等（可选）"
              />
            </label>
          </div>
        </section>

        <section class="form-section">
          <div class="section-number">02</div>
          <div class="section-body">
            <h2>执行策略</h2>
            <div class="mode-picker">
              <label v-for="option in [
                { value: 'auto', title: '自动路由', copy: '由 Supervisor 判断单人或团队' },
                { value: 'single', title: '单 Agent', copy: '适合边界明确的短任务' },
                { value: 'team', title: '多 Agent', copy: '并行分工、验证与修复' },
              ]" :key="option.value">
                <input v-model="form.mode" type="radio" :value="option.value" />
                <span><Bot :size="18" /><strong>{{ option.title }}</strong><small>{{ option.copy }}</small></span>
              </label>
            </div>
            <div class="field-grid">
              <label class="field">
                <span>团队模板</span>
                <select v-model="form.team_template">
                  <option value="software_dev_team">Software Dev Team</option>
                  <option value="research_team">Research Team</option>
                  <option value="analysis_team">Analysis Team</option>
                </select>
              </label>
              <label class="field">
                <span>最大并发</span>
                <input v-model.number="form.max_concurrency" type="number" min="1" max="16" />
              </label>
              <label class="field">
                <span>最大任务数</span>
                <input v-model.number="form.max_tasks" type="number" min="1" max="100" />
              </label>
              <label class="field">
                <span>最大修复轮次</span>
                <input v-model.number="form.max_repair_rounds" type="number" min="0" max="10" />
              </label>
            </div>
          </div>
        </section>

        <section class="form-section">
          <div class="section-number">03</div>
          <div class="section-body">
            <h2>代码仓库</h2>
            <p>绑定本地 Git 仓库后，每个 Coder 使用独立 worktree，集成由控制面串行处理。</p>
            <div class="field-grid">
              <label class="field wide">
                <span><GitBranch :size="14" /> 本地仓库绝对路径</span>
                <input
                  v-model="form.repository_path"
                  type="text"
                  placeholder="/workspace/my-project 或 D:\code\my-project"
                />
              </label>
              <label class="field">
                <span>基础分支</span>
                <input v-model="form.base_branch" type="text" placeholder="main" />
              </label>
            </div>
          </div>
        </section>
      </div>

      <aside class="create-summary">
        <div class="summary-icon"><ShieldCheck :size="22" /></div>
        <h2>治理策略</h2>
        <label class="switch-row">
          <span><strong>需要 Review</strong><small>验证通过后再进入最终状态</small></span>
          <input v-model="form.review_required" type="checkbox" />
        </label>
        <label class="switch-row">
          <span><strong>自动批准低风险操作</strong><small>高风险操作始终需要人工确认</small></span>
          <input v-model="form.auto_approve_low_risk" type="checkbox" />
        </label>
        <dl>
          <div><dt>模式</dt><dd>{{ form.mode }}</dd></div>
          <div><dt>团队</dt><dd>{{ form.team_template }}</dd></div>
          <div><dt>并发</dt><dd>{{ form.max_concurrency }}</dd></div>
          <div><dt>修复预算</dt><dd>{{ form.max_repair_rounds }} 轮</dd></div>
        </dl>
        <div v-if="error" class="notice error">{{ error }}</div>
        <button class="btn btn-primary btn-submit" type="submit" :disabled="!canSubmit">
          <LoaderCircle v-if="submitting" class="spin" :size="17" />
          <Play v-else :size="17" />
          {{ submitting ? "正在创建…" : "创建并开始运行" }}
        </button>
        <small class="submit-note">任务将在后台执行，关闭页面不会中断 Run。</small>
      </aside>
    </form>
  </div>
</template>
