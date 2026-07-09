"""配置加载测试。"""

from app.core.config import Settings, settings


def test_default_config_loads():
    s = Settings()
    assert s.app_name is not None
    assert s.llm_model is not None


def test_dirs_created(tmp_path):
    import os
    os.chdir(tmp_path)
    s = Settings()
    assert os.path.isdir(s.workspace_dir)
    assert os.path.isdir(s.log_dir)


def test_summary_masks_key():
    s = Settings(llm_api_key="sk-1234567890abcdef")
    summary = s.summary()
    assert "sk-123****cdef" in summary
    assert "sk-1234567890abcdef" not in summary
