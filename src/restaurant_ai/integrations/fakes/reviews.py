"""Simulated review platforms.

Generates a mix of sentiment weighted toward the positive, as real review
streams are, but with a reliable trickle of one- and two-star reviews so the
escalation path is genuinely exercised rather than theoretical.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta

from restaurant_ai import clock
from restaurant_ai.integrations.base import ReviewPost

PLATFORMS = ["google", "facebook", "tripadvisor"]

POSITIVE = [
    "The nasi lemak here is the real thing. Rendang was tender and the sambal had proper heat.",
    "Best char kway teow I've had outside Penang. You can taste the wok hei.",
    "Came for lunch with colleagues, food was out fast and everything was hot. Will be back.",
    "Lovely staff, they were patient explaining the menu to my visiting parents.",
    "Laksa lemak was rich without being heavy. Portion size is generous for the price.",
    "Booked for a birthday and they set the table up beautifully without being asked.",
]
NEUTRAL = [
    "Food was decent. Service a bit slow at peak but nothing unreasonable.",
    "Good but not outstanding. Prices have crept up a little since last year.",
    "Solid lunch spot. Parking is the main hassle.",
]
NEGATIVE = [
    "Waited 45 minutes for mains on a Tuesday. Nobody checked on us once.",
    "My rendang arrived cold and the rice was dry. Sent it back and waited again.",
    "I told them clearly about a shellfish allergy and the dish still came with sambal. Not acceptable.",
    "Charged for two teh tarik we never ordered. Had to argue about it at the till.",
    "Table was still dirty when we were seated. Put me off before the food arrived.",
]

AUTHORS = [
    "Adeline T.",
    "Muhammad Faiz",
    "Jasmine Lee",
    "R. Sivakumar",
    "Wong K.H.",
    "Nurul A.",
    "David Chong",
    "Prakash M.",
    "Yee Ling",
    "Hafizah I.",
    "Marcus Tan",
    "Sunita R.",
    "Kelvin Ooi",
    "Aminah B.",
]


class FakeReviews:
    provider = "fake_reviews"

    def __init__(self, seed: int | None = None) -> None:
        self._seed = seed
        self._published: dict[str, str] = {}

    def _rng(self, day) -> random.Random:
        seed = self._seed if self._seed is not None else int(day.strftime("%Y%m%d")) + 7
        return random.Random(seed)

    def generate_day(self, business_date=None) -> list[ReviewPost]:
        business_date = business_date or clock.today()
        rng = self._rng(business_date)
        count = rng.choices([0, 1, 2, 3, 4], weights=[10, 30, 30, 20, 10])[0]

        reviews: list[ReviewPost] = []
        for index in range(count):
            # Weighted toward positive, but negatives appear often enough to
            # keep the escalation path live.
            rating = rng.choices([5, 4, 3, 2, 1], weights=[38, 27, 15, 12, 8])[0]
            if rating >= 4:
                body = rng.choice(POSITIVE)
            elif rating == 3:
                body = rng.choice(NEUTRAL)
            else:
                body = rng.choice(NEGATIVE)

            reviews.append(
                ReviewPost(
                    external_id=f"REV-{business_date.strftime('%y%m%d')}-{index:03d}",
                    platform=rng.choice(PLATFORMS),
                    author=rng.choice(AUTHORS),
                    rating=rating,
                    body=body,
                    posted_at=datetime.combine(
                        business_date,
                        time(rng.randrange(8, 23), rng.randrange(0, 60)),
                        tzinfo=clock.local_tz(),
                    ),
                )
            )
        return reviews

    def fetch_reviews(self, since: datetime) -> list[ReviewPost]:
        """Reviews posted since a point in time, walking back at most a week."""
        collected: list[ReviewPost] = []
        day = since.date()
        end = clock.today()
        while day <= end and (end - day).days <= 7:
            collected.extend(r for r in self.generate_day(day) if r.posted_at >= since)
            day += timedelta(days=1)
        return sorted(collected, key=lambda r: r.posted_at)

    def publish_response(self, review_external_id: str, body: str) -> str:
        self._published[review_external_id] = body
        return f"RESP-{review_external_id}"

    @property
    def published(self) -> dict[str, str]:
        return dict(self._published)
