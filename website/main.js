import "./styles.css";

const translations = {
  zh: {
    "nav.why": "理念",
    "nav.capabilities": "能力",
    "nav.architecture": "架构",
    "nav.quickstart": "快速开始",
    "nav.github": "GitHub",
    "hero.eyebrow": "开源智能体运行时 / V3",
    "hero.title": "一套运行时。<br />一支可信赖的智能体团队。",
    "hero.lede":
      "MegaDeepagents 通过统一的 LangGraph 控制平面，把目标转化为受治理、可恢复的执行过程——从单个智能体到协作团队。",
    "hero.cta": "在 GitHub 上查看",
    "hero.secondary": "了解运行机制",
    "hero.note1": "MIT 开源协议",
    "hero.note2": "模型无关",
    "hero.note3": "支持自托管",
    "runtime.goal": "目标",
    "runtime.goalValue": "交付经过验证的改动",
    "runtime.route": "路由与治理",
    "runtime.focused": "专注执行",
    "runtime.parallel": "并行任务",
    "runtime.evidence": "证据门禁",
    "runtime.event1": "计划已创建",
    "runtime.event2": "智能体已派发",
    "runtime.event3": "产物已验证",
    "proof.graph": "生产级 Root Graph",
    "proof.api": "类型化 API 路径",
    "proof.tests": "后端通过测试",
    "proof.open": "完全开源",
    "why.title": "智能体不应该自己给自己判卷。",
    "why.body":
      "演示往往止步于模型返回文本，而生产工作才刚刚开始。MegaDeepagents 将规划、执行和验证分离，让每次成功运行都具备持久状态、证据和可恢复路径。",
    "why.point1": "Worker 负责产出，Verifier 负责判定成功。",
    "why.point2": "单智能体与团队运行共享同一套执行模型。",
    "why.point3": "每个决策都可以回放、检查和恢复。",
    "cap.title": "为必须完成的工作提供控制平面。",
    "cap.lede": "在不牺牲控制、证据和恢复能力的前提下协调智能体自治。",
    "cap.unified.title": "一张图，承载所有运行",
    "cap.unified.body":
      "复杂度只改变路由，不改变运行时。专注任务和并行团队共享状态、检查点与完成规则。",
    "cap.durable.title": "默认持久可靠",
    "cap.durable.body":
      "SQLite WAL、事件信封和 LangGraph 检查点，让暂停、恢复与进程重启成为一等能力。",
    "cap.verify.title": "先有证据，再谈成功",
    "cap.verify.body": "产物、测试和输出契约必须通过 fail-closed 验证门禁，任务才能完成。",
    "cap.git.title": "隔离的代码工作区",
    "cap.git.body": "受治理的 Git worktree 为每个 Worker 提供独立空间，再执行受控集成。",
    "cap.human.title": "关键位置由人审批",
    "cap.human.body": "计划、权限和高风险操作可以在显式的人机协同检查点暂停。",
    "cap.observe.title": "从源头可观测",
    "cap.observe.body": "类型化事件、SSE 回放、任务图、消息和产物血缘共同解释发生了什么。",
    "architecture.title": "边界简单，执行严谨。",
    "architecture.body":
      "LangGraph 管编排，TaskGraph 管计划，TaskBoard 管执行状态，Worker 产出证据，Verifier 独占成功判定权。",
    "architecture.link": "阅读架构指南",
    "flow.title": "从意图到经过验证的结果。",
    "flow.intake.title": "理解",
    "flow.intake.body": "规范化目标、约束、代码仓库和完成契约。",
    "flow.route.title": "路由",
    "flow.route.body": "选择专注智能体，或构建具备明确角色的团队。",
    "flow.execute.title": "执行",
    "flow.execute.body": "认领依赖已满足的任务，并在隔离环境中工作。",
    "flow.verify.title": "验证",
    "flow.verify.body": "测试输出、检查证据，并在需要时创建真实修复任务。",
    "flow.recover.title": "恢复",
    "flow.recover.body": "暂停、审批、继续或回放，不丢失运行状态。",
    "quickstart.title": "在工作所在的地方运行。",
    "quickstart.body":
      "MegaDeepagents 为自托管而设计。本地启动，自选模型，让代码仓库和运行状态始终由你掌控。",
    "quickstart.copy": "复制",
    "quickstart.ready": "API、运行控制台与持久状态，全部就绪。",
    "cta.title": "构建真正值得信任、能够完成工作的智能体。",
    "cta.github": "查看代码仓库",
    "cta.docs": "阅读文档",
    "footer.line": "面向可信智能体工作的开放运行时。"
  }
};

const defaultEnglish = {};
document.querySelectorAll("[data-i18n]").forEach((element) => {
  defaultEnglish[element.dataset.i18n] = element.innerHTML;
});

const languageToggle = document.querySelector(".language-toggle");
const languageLabels = languageToggle.querySelectorAll("span:not([aria-hidden])");
let language = localStorage.getItem("mega-language") === "zh" ? "zh" : "en";

function applyLanguage(nextLanguage) {
  language = nextLanguage;
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  document.title =
    language === "zh"
      ? "MegaDeepagents — 开源智能体运行时"
      : "MegaDeepagents — Open-source agent runtime";

  document.querySelectorAll("[data-i18n]").forEach((element) => {
    const key = element.dataset.i18n;
    element.innerHTML = language === "zh" ? translations.zh[key] : defaultEnglish[key];
  });

  languageLabels[0].classList.toggle("language-active", language === "en");
  languageLabels[1].classList.toggle("language-active", language === "zh");
  languageToggle.setAttribute(
    "aria-label",
    language === "zh" ? "Switch to English" : "切换到中文"
  );
  localStorage.setItem("mega-language", language);
}

languageToggle.addEventListener("click", () => {
  applyLanguage(language === "en" ? "zh" : "en");
});

const copyButton = document.querySelector(".copy-button");
const command = `git clone https://github.com/xxiaoxiong/MegaDeepagents.git
cd MegaDeepagents
cp .env.example .env
docker compose up --build`;

copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(command);
    const originalKey = "quickstart.copy";
    copyButton.textContent = language === "zh" ? "已复制" : "Copied";
    copyButton.classList.add("is-copied");
    window.setTimeout(() => {
      copyButton.innerHTML =
        language === "zh" ? translations.zh[originalKey] : defaultEnglish[originalKey];
      copyButton.classList.remove("is-copied");
    }, 1600);
  } catch {
    copyButton.textContent = language === "zh" ? "复制失败" : "Copy failed";
  }
});

const revealObserver = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        revealObserver.unobserve(entry.target);
      }
    });
  },
  { threshold: 0.12 }
);

document.querySelectorAll(".reveal").forEach((element) => revealObserver.observe(element));
applyLanguage(language);
