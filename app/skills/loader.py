"""Skill 加载器：扫描 SKILL.md，校验 frontmatter。"""

import re
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger


class SkillInfo:
    def __init__(self, name: str, description: str, path: str, frontmatter: dict[str, Any] | None = None):
        self.name = name
        self.description = description
        self.path = path
        self.frontmatter = frontmatter or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "frontmatter": self.frontmatter,
        }


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """解析 markdown frontmatter。"""
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    fm_text = parts[1].strip()
    body = parts[2].strip()
    frontmatter: dict[str, Any] = {}
    for line in fm_text.split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            # 简单类型推断
            if val.lower() == "true":
                val = True
            elif val.lower() == "false":
                val = False
            elif val.startswith("[") and val.endswith("]"):
                val = [v.strip() for v in val[1:-1].split(",")]
            frontmatter[key] = val
    return frontmatter, body


def validate_skill(frontmatter: dict[str, Any], path: str) -> list[str]:
    """校验 Skill frontmatter，返回错误列表。"""
    errors = []
    if "name" not in frontmatter:
        errors.append("frontmatter 缺少 name 字段")
    if "description" not in frontmatter:
        errors.append("frontmatter 缺少 description 字段")
    if len(frontmatter.get("description", "")) > 1024:
        errors.append("description 超过 1024 字符")
    return errors


class SkillLoader:
    def __init__(self):
        self.skills_dir = Path(settings.skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, SkillInfo] = {}

    def scan(self) -> list[SkillInfo]:
        skills = []
        for skill_dir in self.skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            content = skill_md.read_text(encoding="utf-8")
            frontmatter, body = parse_frontmatter(content)
            errors = validate_skill(frontmatter, str(skill_dir))
            if errors:
                logger.warning(f"Skill {skill_dir.name} invalid: {errors}")
                continue
            info = SkillInfo(
                name=frontmatter.get("name", skill_dir.name),
                description=frontmatter.get("description", ""),
                path=str(skill_dir),
                frontmatter=frontmatter,
            )
            skills.append(info)
            self._cache[info.name] = info
        return skills

    def get(self, name: str) -> SkillInfo | None:
        if name not in self._cache:
            self.scan()
        return self._cache.get(name)

    def read_content(self, name: str) -> str | None:
        info = self.get(name)
        if not info:
            return None
        skill_md = Path(info.path) / "SKILL.md"
        if skill_md.exists():
            return skill_md.read_text(encoding="utf-8")
        return None


_skill_loader: SkillLoader | None = None


def get_skill_loader() -> SkillLoader:
    global _skill_loader
    if _skill_loader is None:
        _skill_loader = SkillLoader()
    return _skill_loader
