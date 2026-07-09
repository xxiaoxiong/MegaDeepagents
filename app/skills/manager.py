"""Skill 管理：创建/更新/校验 skill。"""

from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger
from app.skills.loader import SkillLoader, SkillInfo, parse_frontmatter, validate_skill


class SkillManager:
    def __init__(self, loader: SkillLoader | None = None):
        self.loader = loader or SkillLoader()

    def create_skill(self, name: str, description: str, content: str, author: str = "") -> SkillInfo:
        """创建新 Skill。"""
        skill_dir = Path(settings.skills_dir) / name
        skill_dir.mkdir(parents=True, exist_ok=True)

        frontmatter = f"---\nname: {name}\ndescription: {description}\n"
        if author:
            frontmatter += f"author: {author}\n"
        frontmatter += "---\n\n"

        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(frontmatter + content, encoding="utf-8")

        # 创建子目录
        for sub in ["templates", "references", "scripts", "assets"]:
            (skill_dir / sub).mkdir(exist_ok=True)

        logger.info(f"Skill created: {name} -> {skill_dir}")
        return SkillInfo(name=name, description=description, path=str(skill_dir))

    def validate(self, name: str) -> tuple[bool, list[str]]:
        info = self.loader.get(name)
        if not info:
            return False, ["Skill 不存在"]
        skill_md = Path(info.path) / "SKILL.md"
        if not skill_md.exists():
            return False, ["SKILL.md 不存在"]
        content = skill_md.read_text(encoding="utf-8")
        frontmatter, _ = parse_frontmatter(content)
        errors = validate_skill(frontmatter, info.path)
        return len(errors) == 0, errors


_skill_manager: SkillManager | None = None


def get_skill_manager() -> SkillManager:
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager()
    return _skill_manager
