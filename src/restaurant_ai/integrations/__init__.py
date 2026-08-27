"""External system ports and their implementations.

``get_integrations()`` resolves each port from configuration, so agent code
never knows whether it is talking to a simulator or a live vendor.
"""

from __future__ import annotations

from functools import lru_cache

from restaurant_ai.config import get_settings
from restaurant_ai.integrations.base import Integrations

__all__ = ["Integrations", "get_integrations", "reset_integrations"]


@lru_cache
def get_integrations() -> Integrations:
    """Build the integration set once per process from configuration.

    Every port defaults to its simulator. Setting e.g. POS_PROVIDER=live selects
    the real adapter for that one system, so a deployment can go live on the
    POS while everything else stays simulated.
    """
    settings = get_settings()
    from restaurant_ai.integrations.fakes import (
        FakeBank,
        FakeMessaging,
        FakePayroll,
        FakePOS,
        FakeReviews,
        FakeSocial,
        FakeSupplier,
    )

    def resolve(provider: str, fake, live_factory=None):
        if provider == "fake" or live_factory is None:
            if provider == "live" and live_factory is None:
                raise NotImplementedError(
                    "No live adapter is implemented for this port yet. "
                    "Add one alongside the fake in restaurant_ai/integrations/, "
                    "and keep it behind the same Protocol."
                )
            return fake
        return live_factory()

    return Integrations(
        pos=resolve(settings.pos_provider, FakePOS()),
        messaging=resolve(settings.messaging_provider, FakeMessaging()),
        reviews=resolve(settings.reviews_provider, FakeReviews()),
        supplier=resolve(settings.supplier_provider, FakeSupplier()),
        social=resolve(settings.social_provider, FakeSocial()),
        payroll=resolve(settings.payroll_provider, FakePayroll()),
        bank=resolve(settings.bank_provider, FakeBank()),
        meta={"resolved_from": "config"},
    )


def reset_integrations() -> None:
    """Drop the cached integration set. Used by tests and the simulator."""
    get_integrations.cache_clear()
