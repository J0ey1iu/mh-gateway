from fastapi import APIRouter

component_sources_router = APIRouter(
    prefix="/api/v1/component-sources", tags=["component-sources"]
)

_DEV_COMPONENT_SOURCES = [
    {
        "id": "builtin",
        "label": "Built-in Components",
        "url": "//localhost:5173/component/mh-tool-components.umd.js",
    },
    {
        "id": "extra",
        "label": "Extra Components",
        "url": "//localhost:5173/component/mh-extra-components.umd.js",
    },
]


@component_sources_router.get("")
async def get_component_sources():
    return _DEV_COMPONENT_SOURCES
