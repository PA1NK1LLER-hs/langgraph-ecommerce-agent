"""工具和技能路由。"""
from fastapi import APIRouter

from agent.core import get_all_tools
from skills import list_skills

router = APIRouter(prefix="/api", tags=["tools"])


@router.get("/tools")
async def api_tools():
    tools = get_all_tools()
    safe = []
    for t in tools:
        desc = (t.description or "").split("\n")[0][:200]
        desc = desc.replace("“", "'").replace("”", "'")
        safe.append({"name": t.name, "description": desc})
    return {"count": len(safe), "tools": safe}


@router.get("/skills")
async def api_skills():
    skills = list_skills()
    return {"count": len(skills), "skills": skills}
