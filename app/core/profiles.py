"""Harness Profiles 加载器：从 YAML/JSON 配置注册模型适配配置。"""

import logging
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger

try:
    from deepagents import HarnessProfileConfig, ProviderProfile, register_harness_profile, register_provider_profile

    _DEEPAGENTS_PROFILES = True
except ImportError:  # pragma: no cover
    _DEEPAGENTS_PROFILES = False


def _load_profile_file(path: Path) -> dict[str, Any] | None:
    """从文件加载 YAML/JSON 配置。"""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning(f"读取 profile 文件失败 {path}: {exc}")
        return None
    try:
        import yaml
        return yaml.safe_load(text)
    except Exception:
        try:
            import json
            return json.loads(text)
        except Exception as exc:
            logger.warning(f"解析 profile 文件失败 {path}: {exc}")
            return None
    return None


def _register_harness(data: dict[str, Any], key: str) -> None:
    """注册单个 harness profile。"""
    if not _DEEPAGENTS_PROFILES:
        return
    try:
        config = HarnessProfileConfig.from_dict(data)
        register_harness_profile(key, config)
        logger.info(f"注册 harness profile: {key}")
    except Exception as exc:
        logger.warning(f"注册 harness profile 失败 ({key}): {exc}")


def _register_provider(data: dict[str, Any], key: str) -> None:
    """注册单个 provider profile。"""
    if not _DEEPAGENTS_PROFILES:
        return
    try:
        config = ProviderProfile.from_dict(data)
        register_provider_profile(key, config)
        logger.info(f"注册 provider profile: {key}")
    except Exception as exc:
        logger.warning(f"注册 provider profile 失败 ({key}): {exc}")


def load_profiles(profiles_dir: Path | None = None) -> None:
    """从 profiles/ 目录加载所有 YAML/JSON profile 文件。

    - 文件名形式：`<provider>.yaml` 注册为 provider 级 profile
    - 文件名形式：`<provider>:<model>.yaml` 注册为 provider:model 级 profile
    """
    if profiles_dir is None:
        profiles_dir = Path(settings.runtime_dir) / "profiles"
    if not profiles_dir.exists():
        return

    for path in profiles_dir.glob("*.yaml"):
        stem = path.stem
        if ":" in stem:
            key = stem  # provider:model
        else:
            key = stem  # provider
        data = _load_profile_file(path)
        if not data:
            continue
        if "init_kwargs" in data or "pre_init" in data or "init_kwargs_factory" in data:
            _register_provider(data, key)
        else:
            _register_harness(data, key)

    for path in profiles_dir.glob("*.json"):
        stem = path.stem
        if ":" in stem:
            key = stem
        else:
            key = stem
        data = _load_profile_file(path)
        if not data:
            continue
        if "init_kwargs" in data or "pre_init" in data or "init_kwargs_factory" in data:
            _register_provider(data, key)
        else:
            _register_harness(data, key)


def register_default_profiles() -> None:
    """注册内置默认 profiles，确保基础适配。"""
    if not _DEEPAGENTS_PROFILES:
        return
    # DeepAgents 内置 profiles 由 lazy bootstrap 自动注册；此处可扩展外部 profile
    try:
        load_profiles()
    except Exception as exc:
        logger.warning(f"注册 default profiles 失败: {exc}")
