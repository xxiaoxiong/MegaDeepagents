"""配置管理：从 .env 加载环境变量，自动创建必要目录。"""

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 应用配置
    app_name: str = "MegaDeepagents"
    app_env: str = "dev"
    app_host: str = "127.0.0.1"
    app_port: int = 8081

    # 模型配置
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""
    llm_base_url: str = ""

    # 路径配置
    runtime_dir: str = "./runtime"
    workspace_dir: str = "./runtime/workspace"
    memory_file: str = "./runtime/memory/MEMORY.md"
    user_file: str = "./runtime/memory/USER.md"
    skills_dir: str = "./runtime/skills"
    log_dir: str = "./runtime/logs"
    sqlite_path: str = "./runtime/db/app.sqlite3"
    database_url: str = ""
    workspace_root: str = "./runtime/workspaces"
    sqlite_busy_timeout_ms: int = 5000

    # 功能开关
    enable_web_tools: bool = False
    enable_safe_shell: bool = False
    enable_mcp_tools: bool = False
    enable_legacy_api: bool = False

    # 审批配置
    hitl_required_for_write: bool = True
    hitl_required_for_skill_change: bool = True
    hitl_required_for_memory_change: bool = True

    # ========== 子智能体系统 ==========
    enable_subagents: bool = True
    enable_async_subagents: bool = False
    async_subagent_url: str = "http://127.0.0.1:2024"

    # ========== 沙箱隔离 ==========
    sandbox_provider: str = "none"  # none | local | daytona | modal
    sandbox_root_dir: str = "./runtime/sandbox"
    sandbox_timeout: int = 60

    # ========== 事件流式输出 ==========
    enable_streaming: bool = False
    stream_heartbeat_interval: int = 15

    # ========== 跨线程持久化 ==========
    enable_cross_thread_memory: bool = False
    cross_thread_memory_path: str = "./runtime/store.db"

    # ========== 响应格式结构化 ==========
    enable_response_format: bool = False

    # ========== 辅助 / 反思模型（非主链路，留空则回退主模型）==========
    aux_llm_provider: str = ""
    aux_llm_model: str = ""
    aux_llm_api_key: str = ""
    aux_llm_base_url: str = ""
    reflection_llm_provider: str = ""
    reflection_llm_model: str = ""
    reflection_llm_api_key: str = ""
    reflection_llm_base_url: str = ""

    # ========== LLM 缓存 ==========
    enable_llm_cache: bool = True
    llm_cache_path: str = "./runtime/cache/llm_cache.db"

    # ========== 安全加固 ==========
    # Never default to ``["*"]`` together with ``allow_credentials=True``:
    # Starlette reflects the Origin header under that combination, which is
    # equivalent to whitelisting any malicious page.  The local dev frontend
    # (Vite on 5173) is the only safe default; production deployments must set
    # ``CORS_ORIGINS`` explicitly.
    cors_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]
    # Optional bearer token guarding control-plane decisions (permission /
    # plan approval).  When unset, decision endpoints are only reachable from
    # loopback and remain open in single-user dev.  When set, callers must send
    # ``Authorization: Bearer <token>``.  Required for any non-loopback /
    # Docker / reverse-proxy deployment.
    control_plane_api_token: str = ""
    rate_limit_per_minute: int = 100
    max_message_length: int = 50000
    pending_runner_ttl_minutes: int = 30
    max_concurrency: int = 4
    max_team_size: int = 6
    max_spawn_depth: int = 2
    max_repair_rounds: int = 5
    # 900s left planning tasks stuck for 15 min before the scheduler could
    # re-dispatch failed siblings.  300s is long enough for a coding/test
    # turn yet short enough that a stuck task doesn't paralyse the team.
    task_execution_timeout_seconds: float = 300.0
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 60.0
    # 429 backoff: LLM gateways throttle for 10-60s; a 2s base delay retries
    # while the throttle is still active and burns the retry budget.  15s
    # base / 300s cap gives the window room to clear.
    retry_rate_limit_base_delay_seconds: float = 15.0
    retry_rate_limit_max_delay_seconds: float = 300.0
    stalled_run_threshold_seconds: int = 180
    audit_heartbeat_interval_seconds: float = 15.0
    default_auto_approve_low_risk: bool = False
    frontend_origin: str = "http://127.0.0.1:5173"
    # Global cap on simultaneously active Runs.  ``asyncio.to_thread`` occupies
    # a host thread for the entire Run duration (minutes–hours); without a
    # cap, Run #33+ exhausts the default executor (``min(32, cpu+4)``) and
    # starves synchronous API endpoints (head-of-line blocking).  The cap is
    # enforced by a semaphore in ``TeamRuntimeFacade``; surplus Runs queue
    # instead of consuming threads.  Operators scaling horizontally should
    # raise this alongside the executor pool size.
    max_concurrent_runs: int = 8

    # ========== LangSmith 可观测性（可选；未配置时本地可跑） ==========
    langsmith_enabled: bool = False  # 总开关，默认 False 满足"未配置本地可跑"约束
    langsmith_api_key: str = ""  # 留空则即便 enabled=True 也降级 offline_log
    langsmith_project: str = "multiagent-frame"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_tracing: bool = True  # enabled 且有 key 时是否真的发样本
    langsmith_service_name: str = "multiagent-frame"
    langsmith_sample_rate: float = 1.0  # [0,1] 热路径采样率
    langsmith_offline_log: bool = True  # 关闭时是否把等价 trace 摘要打到本地日志

    def _ensure_dirs(self) -> None:
        for d in [
            self.runtime_dir,
            self.workspace_dir,
            self.log_dir,
            Path(self.memory_file).parent,
            Path(self.user_file).parent,
            self.skills_dir,
            Path(self.sqlite_path).parent,
            Path(self.cross_thread_memory_path).parent,
            Path(self.llm_cache_path).parent,
            Path(self.skills_dir) / ".archive",
            Path(self.skills_dir) / ".snapshots",
            self.runtime_dir + "/profiles",
        ]:
            Path(d).mkdir(parents=True, exist_ok=True)

    def model_post_init(self, context) -> None:
        if self.database_url.startswith("sqlite:///"):
            self.sqlite_path = self.database_url.removeprefix("sqlite:///")
        elif not self.database_url:
            self.database_url = f"sqlite:///{self.sqlite_path}"
        # ``allow_credentials=True`` + ``cors_origins=["*"]`` causes Starlette
        # to reflect the request Origin header, which is equivalent to
        # whitelisting every origin.  Refuse to start in that state rather
        # than silently widening the CORS surface.
        if "*" in self.cors_origins:
            # Drop the wildcard and fall back to the loopback dev origins so
            # that a stale ``CORS_ORIGINS=*`` env override cannot reopen the
            # reflection hole; log nothing here (settings is pre-logging) —
            # the main app startup warns about non-loopback exposure.
            self.cors_origins = ["http://127.0.0.1:5173", "http://localhost:5173"]
        self._ensure_dirs()
        # deepagents 0.6.8 内部走 `init_chat_model("openai:<model>")` 路径，
        # 无法显式注入 api_key/base_url；这里把 OpenAI 兼容的凭证写到环境变量，
        # 供 deepagents 与 langchain-openai 默认读取。
        if self.llm_provider.lower() == "openai-compatible":
            os.environ.setdefault("OPENAI_API_KEY", self.llm_api_key or "no-key")
            if self.llm_base_url:
                os.environ.setdefault("OPENAI_BASE_URL", self.llm_base_url)

    @property
    def resolved_model(self) -> str:
        if self.llm_provider.lower() == "openai-compatible" and self.llm_base_url:
            return f"openai:{self.llm_model}"
        return f"{self.llm_provider}:{self.llm_model}"

    def summary(self) -> str:
        key = self.llm_api_key
        masked = (
            key[:6] + "****" + key[-4:] if len(key) > 10 else ("****" if key else "(not set)")
        )
        return (
            f"AppName: {self.app_name}\n"
            f"AppEnv: {self.app_env}\n"
            f"LLMProvider: {self.llm_provider}\n"
            f"LLMModel: {self.llm_model}\n"
            f"APIKey: {masked}\n"
            f"Workspace: {self.workspace_dir}\n"
            f"Memory: {self.memory_file}\n"
            f"SkillsDir: {self.skills_dir}\n"
            f"SQLitePath: {self.sqlite_path}\n"
            f"WebTools: {self.enable_web_tools}\n"
            f"SafeShell: {self.enable_safe_shell}\n"
            f"MCPTools: {self.enable_mcp_tools}\n"
            f"HITLWrite: {self.hitl_required_for_write}\n"
            f"SubAgents: {self.enable_subagents}\n"
            f"AsyncSubAgents: {self.enable_async_subagents} ({self.async_subagent_url})\n"
            f"Sandbox: {self.sandbox_provider} (root={self.sandbox_root_dir}, timeout={self.sandbox_timeout}s)\n"
            f"Streaming: {self.enable_streaming}\n"
            f"CrossThreadMemory: {self.enable_cross_thread_memory}\n"
            f"ResponseFormat: {self.enable_response_format}\n"
            f"LLMCache: {self.enable_llm_cache} ({self.llm_cache_path})\n"
            f"LangSmith: enabled={self.langsmith_enabled} project={self.langsmith_project} "
            f"tracing={self.langsmith_tracing} sample_rate={self.langsmith_sample_rate} "
            f"offline_log={self.langsmith_offline_log}\n"
            f"CORSOrigins: {self.cors_origins}\n"
            f"RateLimit: {self.rate_limit_per_minute} req/min\n"
            f"MaxMessageLength: {self.max_message_length}\n"
            f"PendingRunnerTTL: {self.pending_runner_ttl_minutes} min\n"
            f"TaskTimeout: {self.task_execution_timeout_seconds}s\n"
            f"RetryBackoff: {self.retry_base_delay_seconds}s..{self.retry_max_delay_seconds}s\n"
            f"StalledThreshold: {self.stalled_run_threshold_seconds}s\n"
        )


settings = Settings()

if __name__ == "__main__":
    print(settings.summary())
