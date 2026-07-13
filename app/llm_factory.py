"""模型接入：根据配置初始化 Chat Model。"""

from langchain.chat_models import init_chat_model

from app.core.config import settings
from app.core.logging import logger


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
        return init_chat_model(f"openai:{model}", api_key=api_key or "no-key", base_url=base_url)
    return init_chat_model(f"{provider}:{model}")


def build_model():
    """主模型。"""
    s = settings
    if s.llm_provider.lower() == "openai-compatible" and s.llm_base_url:
        logger.info(f"Using OpenAI-compatible model: {s.llm_model} at {s.llm_base_url}")
        return init_chat_model(
            f"openai:{s.llm_model}",
            api_key=s.llm_api_key or "no-key",
            base_url=s.llm_base_url,
        )
    if s.llm_provider.lower() == "deepseek" or s.llm_model.startswith("deepseek:"):
        api_key = s.llm_api_key
        if not api_key:
            logger.warning("DeepSeek API Key 未设置，使用占位符")
            api_key = "sk-placeholder"
        logger.info(f"Using DeepSeek model: {s.llm_model}")
        return _build_deepseek(s.llm_model, api_key)
    logger.info(f"Using generic model: {s.resolved_model}")
    return init_chat_model(s.resolved_model)


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
        if max_tokens and max_tokens != 4096:
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
