from fastapi import APIRouter

from app.api.routes import deployments, health, logs, services, simulation


def build_api_router(include_simulation_control: bool) -> APIRouter:
    router = APIRouter()
    router.include_router(health.router)
    router.include_router(services.router)
    router.include_router(logs.router)
    router.include_router(deployments.router)
    if include_simulation_control:
        router.include_router(simulation.router)
    return router
