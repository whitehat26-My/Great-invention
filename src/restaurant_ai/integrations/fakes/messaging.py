"""Simulated WhatsApp/web/phone messaging.

Produces the free-text booking and ordering requests the front-of-house agents
have to interpret: relative dates, vague party sizes, dietary constraints
buried mid-sentence.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta

from restaurant_ai import clock
from restaurant_ai.integrations.base import InboundMessage

BOOKING_REQUESTS = [
    "Hi, do you have a table for 4 tonight around 7:30?",
    "Table for two this Friday 8pm please, window seat if possible",
    "Can I book 6 people Saturday evening? One is vegetarian.",
    "Any space for 2 tomorrow lunch? Around 12:30",
    "Hi we're a party of 8 for my mother's birthday next Saturday, is that possible?",
    "Do you take walk-ins on a Wednesday night or should I book?",
    "Need to change my booking tonight from 4 to 5 people, is that ok?",
    "Table for 3 at 6pm today please. One severe peanut allergy, is that manageable?",
]

ORDER_REQUESTS = [
    "Can I order 2 nasi lemak and a teh tarik for pickup in 20 mins?",
    "One beef rendang rice and one laksa, no shellfish in the laksa please",
    "Two char kway teow, one without egg, and 2 iced lemon tea",
    "Delivery please: 3 nasi lemak, extra sambal on the side",
]

SENDERS = [
    "+60122334455",
    "+60193456789",
    "+60177654321",
    "+60165551234",
    "+60112223344",
    "+60198887766",
]
NAMES = ["Aisyah", "Kevin", "Meera", "Zul", "Chloe", "Ганеш", "Farhan", "Li Wei"]


class FakeMessaging:
    provider = "fake_messaging"

    def __init__(self, seed: int | None = None) -> None:
        self._seed = seed
        self._sent: list[tuple[str, str]] = []

    def _rng(self, day) -> random.Random:
        seed = self._seed if self._seed is not None else int(day.strftime("%Y%m%d")) + 11
        return random.Random(seed)

    def generate_day(self, business_date=None) -> list[InboundMessage]:
        business_date = business_date or clock.today()
        rng = self._rng(business_date)
        count = rng.randrange(2, 7)

        messages: list[InboundMessage] = []
        for index in range(count):
            is_booking = rng.random() < 0.7
            body = rng.choice(BOOKING_REQUESTS if is_booking else ORDER_REQUESTS)
            messages.append(
                InboundMessage(
                    external_id=f"MSG-{business_date.strftime('%y%m%d')}-{index:03d}",
                    channel=rng.choices(["whatsapp", "web", "phone"], weights=[70, 20, 10])[0],
                    sender=rng.choice(SENDERS),
                    body=body,
                    received_at=datetime.combine(
                        business_date,
                        time(rng.randrange(9, 21), rng.randrange(0, 60)),
                        tzinfo=clock.local_tz(),
                    ),
                    guest_name=rng.choice(NAMES),
                )
            )
        return sorted(messages, key=lambda m: m.received_at)

    def fetch_messages(self, since: datetime) -> list[InboundMessage]:
        collected: list[InboundMessage] = []
        day = since.date()
        end = clock.today()
        while day <= end and (end - day).days <= 3:
            collected.extend(m for m in self.generate_day(day) if m.received_at >= since)
            day += timedelta(days=1)
        return sorted(collected, key=lambda m: m.received_at)

    def send_message(self, recipient: str, body: str) -> str:
        self._sent.append((recipient, body))
        return f"MSG-OUT-{len(self._sent):05d}"

    @property
    def sent(self) -> list[tuple[str, str]]:
        return list(self._sent)
