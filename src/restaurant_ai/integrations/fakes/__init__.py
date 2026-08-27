"""In-repo simulators for every external system.

Seeded from the business date, so a given day always produces the same orders,
reviews, deliveries and settlements. That determinism is what lets the simulated
service day double as a regression test.
"""

from restaurant_ai.integrations.fakes.bank import FakeBank
from restaurant_ai.integrations.fakes.messaging import FakeMessaging
from restaurant_ai.integrations.fakes.payroll import FakePayroll
from restaurant_ai.integrations.fakes.pos import FakePOS
from restaurant_ai.integrations.fakes.reviews import FakeReviews
from restaurant_ai.integrations.fakes.social import FakeSocial
from restaurant_ai.integrations.fakes.supplier import FakeSupplier

__all__ = [
    "FakeBank",
    "FakeMessaging",
    "FakePayroll",
    "FakePOS",
    "FakeReviews",
    "FakeSocial",
    "FakeSupplier",
]
