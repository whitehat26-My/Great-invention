"""Front of house: reputation.

Reservations and conversational ordering were retired. Both talked to guests
live, under their own names, and the order agent gave allergen advice — a wrong
answer there is a different kind of wrong from a wrong purchase order. The
platform is a management tool; nothing in it now writes to a guest unprompted.

The reputation agent stays because it is already that shape: it drafts replies
and a human sends them.
"""

from restaurant_ai.agents.front_of_house.reputation import REPUTATION_AGENT

__all__ = ["REPUTATION_AGENT"]
