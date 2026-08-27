"""Human approval: the gate between an agent proposing and an agent acting."""

from restaurant_ai.approvals.service import dispatch, list_pending, resolve

__all__ = ["dispatch", "list_pending", "resolve"]
