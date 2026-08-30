"""The FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from restaurant_ai import __version__
from restaurant_ai.api.routes import (
    agents_router,
    approvals_router,
    dashboard_router,
    health_router,
)
from restaurant_ai.api.webhooks import router as webhooks_router
from restaurant_ai.config import get_settings
from restaurant_ai.kernel.registry import all_agents
from restaurant_ai.logging_setup import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    log.info(
        "api starting",
        restaurant=settings.restaurant_name,
        llm=settings.llm_provider,
        approvals=settings.approval_channel,
    )
    yield
    log.info("api stopping")


app = FastAPI(
    title="Autonomous Restaurant Operations",
    version=__version__,
    # Counted, not written down. This claimed two more than existed for months
    # after two were retired, which is the failure mode of every number kept in
    # prose: nothing breaks, and the docs quietly stop being true.
    description=(
        f"Event ingestion and control plane for an AI-operated restaurant: "
        f"{len(all_agents())} agents across front of house, kitchen, supply chain, "
        f"marketing, workforce and finance."
    ),
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(webhooks_router)
app.include_router(agents_router)
app.include_router(approvals_router)
app.include_router(dashboard_router)


@app.get("/", tags=["health"])
async def root() -> dict[str, str]:
    return {
        "service": "restaurant-ai",
        "version": __version__,
        "docs": "/docs",
    }
