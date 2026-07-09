"""Skills routes: 查询和注册 Skills。"""

from fastapi import APIRouter, Body
from pydantic import BaseModel

from app.core.logging import logger
from app.skills.loader import get_skill_loader
from app.skills.metadata import list_skills, register_skill, get_skill, update_skill_state

router = APIRouter()


class SkillCreateBody(BaseModel):
    name: str
    description: str
    content: str
    created_by: str = "user"
    source: str = "local"


class PinBody(BaseModel):
    pinned: bool


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
    loader = get_skill_loader()
    content = loader.read_content(name)
    if content is None:
        return {"error": f"Skill '{name}' not found"}
    return {"name": name, "content": content}


@router.post("/skills")
def create_skill(body: SkillCreateBody):
    from pathlib import Path
    from app.core.config import settings
    skill_dir = Path(settings.skills_dir) / body.name
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
    updated = update_skill_state(name, pinned=body.pinned)
    if not updated:
        return {"error": "Skill not found"}
    return {"status": "ok", "pinned": body.pinned}
