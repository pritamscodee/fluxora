from fastapi import APIRouter

from app.api.v1 import (
    agent,
    assets,
    auth,
    delivery,
    devstore,
    projects,
    signing_keys,
    uploads,
    webhooks,
)
from app.core.config import settings

api_router = APIRouter(prefix="/v1")

api_router.include_router(auth.router)
api_router.include_router(projects.router)
api_router.include_router(webhooks.router)
api_router.include_router(signing_keys.router)
api_router.include_router(agent.router)
api_router.include_router(uploads.router)
api_router.include_router(assets.router)
api_router.include_router(delivery.router)

if not settings.storage_configured:
    api_router.include_router(devstore.router)
