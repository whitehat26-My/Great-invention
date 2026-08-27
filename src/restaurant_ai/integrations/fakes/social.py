"""Simulated social scheduling. Records what would have been published."""

from __future__ import annotations

from restaurant_ai.integrations.base import ScheduledPost


class FakeSocial:
    provider = "fake_social"

    def __init__(self) -> None:
        self._scheduled: list[ScheduledPost] = []

    def schedule_post(self, post: ScheduledPost) -> str:
        ref = f"SOC-{len(self._scheduled) + 1:05d}"
        post.external_ref = ref
        self._scheduled.append(post)
        return ref

    def fetch_scheduled(self) -> list[ScheduledPost]:
        return list(self._scheduled)
