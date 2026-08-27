"""Inbound webhooks from POS, payments, messaging, reviews and delivery.

The module is named ``routes`` rather than ``router`` so that importing it does
not collide with the ``router`` object exported here — a package attribute that
shadows a submodule of the same name is a trap.
"""

from restaurant_ai.api.webhooks.routes import router

__all__ = ["router"]
