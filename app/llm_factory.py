"""模型接入：根据配置初始化 Chat Model。"""

from langchain.chat_models import init_chat_model

from app.core.config import settings
from app.core.logging import logger


# deepagents 0.6.8 内置 "openai" provider profile 默认 init_kwargs={"use_responses_api": True},
# 对很多第三方 OpenAI 兼容端点（如部分代理）会触发 Responses API 与 429/500；
# 在这里把默认 profile 覆盖为 chat.completions 协议。
def _install_deepagents_openai_profile_override() -> None:
    try:
        from deepagents import ProviderProfile, register_provider_profile
        register_provider_profile(
            "openai",
            ProviderProfile(init_kwargs={
                "use_responses_api": False,
                "request_timeout": 600.0,
                "max_retries": 2,
                "streaming": True,
                # DeepSeek-Chat default max_tokens is 4096, which truncates
                # long planning documents mid-sentence (e.g. "日期处理" ending
                # at "d").  8192 is the model's max output and gives the LLM
                # enough room to complete architecture/design artifacts.
                "max_tokens": 8192,
            }),
        )
    except Exception as exc:
        logger.debug(f"deepagents openai profile override skipped: {exc}")


_install_deepagents_openai_profile_override()


# deepagents 0.6.8 的 create_deep_agent 内部会调用
# ``init_chat_model(model, **apply_provider_profile(model))``，而
# ``apply_provider_profile`` 假设 model 是字符串 spec（调用 model.count(":")）。
# 当我们把已实例化且配好 streaming=True / request_timeout=600 的 ChatOpenAI
# 直接传给 create_deep_agent 时（为了避免内部走 init_chat_model 重建实例丢
# 这些连接层参数），apply_provider_profile 会因 ``'ChatOpenAI' object has no
# attribute 'count'`` 直接抛 AttributeError。这里在模块加载阶段一次 monkey-patch：
# 当 spec 不是字符串时返回空 kwargs，让 init_chat_model 完全跳过 provider
# profile 合并、直接复用我们传入的 model 实例。
def _patch_apply_provider_profile_handles_model_instances() -> None:
    try:
        from deepagents.profiles import provider as _provider_mod
    except Exception as exc:  # pragma: no cover - 仅在 deepagents 版本变时失配
        logger.debug(f"deepagents apply_provider_profile patch skipped: {exc}")
        return

    wrapped = getattr(_provider_mod, "apply_provider_profile", None)
    if wrapped is None or getattr(wrapped, "__mda_patched__", False):
        return

    def _safe(spec):
        # 仅字符串 spec 才走原 provider profile 解析；其它形式（如已实例化
        # 的 ChatOpenAI / ChatDeepSeek / ChatAnthropic 等 Runnable）跳过合并。
        if isinstance(spec, str):
            return wrapped(spec)
        return {}

    _safe.__mda_patched__ = True
    _provider_mod.apply_provider_profile = _safe

    # 同时替换 deepagents 顶层命名空间中的引用，确保 create_deep_agent
    # 内 ``from deepagents import apply_provider_profile`` 路径也走 patched 版。
    try:
        import deepagents as _deepagents_top
        if getattr(_deepagents_top, "apply_provider_profile", None) is wrapped:
            _deepagents_top.apply_provider_profile = _safe
    except Exception:
        pass


_patch_apply_provider_profile_handles_model_instances()


def _build_deepseek(model: str, api_key: str):

    """延迟导入 ChatDeepSeek，避免在未安装 langchain_deepseek 时整个模块加载失败。

    DeepSeek API 与 OpenAI 协议兼容，缺包时自动 fallback 到 openai provider。
    """
    try:
        from langchain_deepseek import ChatDeepSeek
    except ImportError:
        logger.warning(
            "langchain_deepseek 未安装，DeepSeek 模型 fallback 到 OpenAI 兼容协议。"
            "可执行 `pip install langchain-deepseek` 启用原生集成。"
        )
        return init_chat_model(
            f"openai:{model}",
            api_key=api_key or "no-key",
            base_url="https://api.deepseek.com/v1",
        )
    return ChatDeepSeek(model=model, api_key=api_key)


def _build_for_provider(provider: str, model: str, api_key: str, base_url: str):
    if not provider or not model:
        return None
    if provider.lower() == "deepseek" or model.startswith("deepseek:"):
        key = api_key or "sk-placeholder"
        return _build_deepseek(model, key)
    if base_url:
        return init_chat_model(
            f"openai:{model}",
            api_key=api_key or "no-key",
            base_url=base_url,
            request_timeout=600.0,
            max_retries=2,
            use_responses_api=False,
            streaming=True,
        )
    return init_chat_model(
        f"openai:{model}",
        request_timeout=600.0,
        max_retries=2,
        use_responses_api=False,
        streaming=True,
    )


def build_model():
    """主模型。"""
    s = settings
    # streaming=True 让 ChatOpenAI 走 stream 模式逐 token 返回，
    # 避免推理慢的 reasoning 模型在长 idle 时被上游网关断 socket。
    common = {
        "use_responses_api": False,
        "request_timeout": 600.0,
        "max_retries": 2,
        "streaming": True,
    }
    if s.llm_provider.lower() == "openai-compatible" and s.llm_base_url:
        logger.info(f"Using OpenAI-compatible model: {s.llm_model} at {s.llm_base_url}")
        return init_chat_model(
            f"openai:{s.llm_model}",
            api_key=s.llm_api_key or "no-key",
            base_url=s.llm_base_url,
            **common,
        )
    if s.llm_provider.lower() == "deepseek" or s.llm_model.startswith("deepseek:"):
        api_key = s.llm_api_key
        if not api_key:
            logger.warning("DeepSeek API Key 未设置，使用占位符")
            api_key = "sk-placeholder"
        logger.info(f"Using DeepSeek model: {s.llm_model}")
        return _build_deepseek(s.llm_model, api_key)
    logger.info(f"Using generic model: {s.resolved_model}")
    return init_chat_model(s.resolved_model, **common)


def build_model_for_policy(model_policy) -> "Any":
    """根据 AgentProfile.model_policy 装饰模型。

    让 profile 真正影响模型选择（任务书 §23#15）。
    设计：始终从 build_model() 出发（确保测试 monkeypatch 路径不中断），
    然后应用 model_policy 上的 temperature / max_tokens / timeout 超参。

    provider / model_name 的切换通过 build_model() 内部的 settings 实现；
    本函数只对返回值做 bind 修饰。
    profile 如需切换 provider：通过 settings 的 llm_provider / llm_model 控制，
    或通过 monkeypatch build_model 注入。

    Args:
        model_policy: AgentProfile.model_policy（ModelPolicy 实例），可为 None。
    """
    if model_policy is None:
        return build_model()

    model = build_model()
    temperature = getattr(model_policy, "temperature", None)
    max_tokens = getattr(model_policy, "max_tokens", None)
    timeout = getattr(model_policy, "timeout_seconds", None)

    try:
        kwargs: dict = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if timeout and timeout != 60.0:
            kwargs["request_timeout"] = timeout
        if kwargs and hasattr(model, "bind"):
            model = model.bind(**kwargs)
    except Exception as exc:
        logger.debug(f"build_model_for_policy: bind 超参失败（忽略）: {exc}")

    return model


def build_aux_model():
    """辅助模型（用于 Curator/Evolution 等非主链路）。"""
    s = settings
    m = _build_for_provider(s.aux_llm_provider, s.aux_llm_model, s.aux_llm_api_key, s.aux_llm_base_url)
    if m is None:
        logger.info("Aux model not configured, falling back to main model")
        return build_model()
    logger.info(f"Using aux model: {s.aux_llm_provider}/{s.aux_llm_model}")
    return m


def build_reflection_model():
    """反思/评测模型。"""
    s = settings
    m = _build_for_provider(s.reflection_llm_provider, s.reflection_llm_model, s.reflection_llm_api_key, s.reflection_llm_base_url)
    if m is None:
        logger.info("Reflection model not configured, falling back to aux model")
        return build_aux_model()
    logger.info(f"Using reflection model: {s.reflection_llm_provider}/{s.reflection_llm_model}")
    return m


def build_deepagents_model_spec(model_policy=None) -> str:
    """为 deepagents 0.6.8 的 create_deep_agent 构建字符串 spec。

    deepagents 0.6.8 在解析 model 时会再次调用
    ``init_chat_model(model, **apply_provider_profile(model))``：当 model 是
    字符串 spec 时正常工作，当 model 是已实例化的 ChatOpenAI 时会在
    ``apply_provider_profile`` 中走 ``model.count(":")`` 报错。因此对 deepagents
    始终传 spec 字符串，凭证通过 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` 环境
    变量下发（见 ``app.core.config.Settings.model_post_init``）。
    """
    s = settings
    if s.llm_provider.lower() == "openai-compatible" and s.llm_base_url:
        return f"openai:{s.llm_model}"
    if s.llm_provider.lower() in {"deepseek", "openai", "deepseek-chat", "openai-compatible"}:
        return f"openai:{s.llm_model}"
    return s.resolved_model
