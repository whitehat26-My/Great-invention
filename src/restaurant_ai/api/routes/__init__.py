"""Control and observability routes."""

from restaurant_ai.api.routes.agents import router as agents_router
from restaurant_ai.api.routes.approvals import router as approvals_router
from restaurant_ai.api.routes.dashboard import router as dashboard_router
from restaurant_ai.api.routes.health import router as health_router

__all__ = ["agents_router", "approvals_router", "dashboard_router", "health_router"]
