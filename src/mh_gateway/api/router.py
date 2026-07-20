from fastapi import APIRouter

from mh_gateway.api.agents import router as agents_router
from mh_gateway.api.auth_routes import auth_router
from mh_gateway.api.chat import router as chat_router
from mh_gateway.api.guide import router as guide_router
from mh_gateway.api.management import router as management_router
from mh_gateway.api.runtime_tools import router as runtime_tools_router
from mh_gateway.api.scenarios import router as scenarios_router
from mh_gateway.api.sessions import router as sessions_router
from mh_gateway.api.tools import router as tools_router
from mh_gateway.monitoring.api import metrics_router
from mh_gateway.monitoring.health import health_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(chat_router)
router.include_router(scenarios_router)
router.include_router(sessions_router)
router.include_router(guide_router)
router.include_router(agents_router)
router.include_router(tools_router)
router.include_router(runtime_tools_router)
router.include_router(management_router)
router.include_router(health_router)
router.include_router(metrics_router)
