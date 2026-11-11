from fastapi import APIRouter, Depends

from app.api import (
    agui,
    auth,
    chats,
    email_auth,
    email_skill,
    files,
    health,
    local_memories,
    memories,
    users,
    video,
    voice,
)
from app.api.dependencies import verify_bearer

router = APIRouter()


router.include_router(auth.router)


router.include_router(email_auth.router)


router.include_router(users.router)


router.include_router(voice.router)


router.include_router(health.router)


protected = APIRouter(dependencies=[Depends(verify_bearer)])


protected.include_router(agui.router)


protected.include_router(memories.router)


protected.include_router(local_memories.router)


protected.include_router(email_skill.router)


protected.include_router(video.router)


protected.include_router(files.router)


protected.include_router(chats.router)


router.include_router(protected)
