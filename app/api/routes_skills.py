"""Skills routes: 查询和注册 Skills。"""

import re
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, field_validator

from app.core.logging import logger
from app.skills.loader import get_skill_loader
from app.skills.metadata import list_skills, register_skill, get_skill, update_skill_state

router = APIRouter()

# Skill names are used directly as filesystem subdirectories under
# ``settings.skills_dir``.  Reject anything outside ``[A-Za-z0-9_-]`` so
# ``../../etc/cron.d/pwned`` cannot create a directory outside the skills
# root.  Keep the pattern tight: dots, spaces, and shell metacharacters are
# all disallowed.
_SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class SkillCreateBody(BaseModel):
    name: str
    description: str
    content: str
    created_by: str = "user"
    source: str = "local"

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or len(value) > 64 or not _SKILL_NAME_PATTERN.match(value):
            raise ValueError(
                "name must be 1-64 characters of [A-Za-z0-9_-] only"
            )
        return value


class PinBody(BaseModel):
    pinned: bool


def _ensure_safe_skill_name(name: str) -> str:
    """Defense-in-depth check used by every name-bearing endpoint."""
    if not name or len(name) > 64 or not _SKILL_NAME_PATTERN.match(name):
        raise HTTPException(status_code=422, detail="invalid skill name")
    return name


@router.get("/skills")
def list_skills_api():
    loader = get_skill_loader()
    scanned = loader.scan()
    metas = list_skills()
    meta_map = {m["name"]: m for m in metas}
    result = []
    for s in scanned:
        m = meta_map.get(s.name, {})
        result.append({
            "name": s.name,
            "description": s.description,
            "path": s.path,
            "frontmatter": s.to_dict().get("frontmatter", {}),
            "created_by": m.get("created_by", "user"),
            "state": m.get("state", "active"),
            "pinned": m.get("pinned", False),
            "version": m.get("version", 1),
        })
    return {"skills": result, "count": len(result)}


@router.get("/skills/{name}")
def read_skill(name: str):
    _ensure_safe_skill_name(name)
    loader = get_skill_loader()
    content = loader.read_content(name)
    if content is None:
        return {"error": f"Skill '{name}' not found"}
    return {"name": name, "content": content}


@router.post("/skills")
def create_skill(body: SkillCreateBody):
    from app.core.config import settings
    # Re-check even though pydantic validated: the field_validator only runs
    # when the body is parsed as JSON; defensive depth costs nothing here.
    _ensure_safe_skill_name(body.name)
    skills_root = Path(settings.skills_dir).resolve()
    skill_dir = (skills_root / body.name).resolve()
    # Final safety net: ensure the resolved path is still inside skills_root.
    # A weird filesystem layout (symlinks, case-insensitive mounts) could
    # otherwise let a "valid" name escape.
    if not skill_dir.is_relative_to(skills_root):
        raise HTTPException(status_code=422, detail="invalid skill name")
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    frontmatter = f"---\nname: {body.name}\ndescription: {body.description}\n"
    skill_md.write_text(frontmatter + "\n\n" + body.content, encoding="utf-8")
    for sub in ["templates", "references", "scripts", "assets"]:
        (skill_dir / sub).mkdir(exist_ok=True)

    meta = register_skill(
        name=body.name,
        path=skill_dir,
        description=body.description,
        created_by=body.created_by,
        source=body.source,
    )
    return {"status": "created", "skill": meta}


@router.post("/skills/{name}/pin")
def pin_skill(name: str, body: PinBody):
    _ensure_safe_skill_name(name)
    updated = update_skill_state(name, pinned=body.pinned)
    if not updated:
        return {"error": "Skill not found"}
    return {"status": "ok", "pinned": body.pinned}
