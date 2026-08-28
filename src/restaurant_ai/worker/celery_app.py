"""Celery application and the operating rhythm.

The beat schedule is the restaurant's day: prep forecast before the kitchen
arrives, reorder sweeps morning and afternoon, review sweeps hourly, and the
books closed after service. Each entry names the agent the registry resolves, so
the schedule and the agents cannot drift apart.

Times are local (the restaurant's timezone), because "23:30" has to mean 23:30
where the restaurant is, not in UTC.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from restaurant_ai.config import get_settings

settings = get_settings()

celery_app = Celery(
    "restaurant_ai",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["restaurant_ai.worker.tasks"],
)

celery_app.conf.update(
    timezone=settings.timezone,
    enable_utc=False,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_time_limit=600,
    task_soft_time_limit=540,
    worker_max_tasks_per_child=200,
    task_acks_late=True,  # a worker that dies mid-task leaves the job redeliverable
    task_reject_on_worker_lost=True,
    task_always_eager=settings.celery_task_always_eager,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
)

# agent name -> when it runs. The registry supplies the agent itself.
SCHEDULE: dict[str, tuple[crontab, str]] = {
    "prep_forecaster": (
        crontab(hour="6", minute="0"),
        "Morning prep forecast, before the kitchen arrives.",
    ),
    "stock_reorder": (
        crontab(hour="7,15", minute="0"),
        "Reorder sweep: after the morning delivery and again mid-afternoon.",
    ),
    "supplier_invoice": (
        crontab(hour="9", minute="30"),
        "Book in deliveries and three-way match yesterday's invoices.",
    ),
    "reputation": (
        crontab(minute="15"),
        "Hourly review sweep.",
    ),
    "order_pacing": (
        crontab(minute="*/5", hour="11-23"),
        "Route and pace open tickets during service.",
    ),
    "social_content": (
        crontab(hour="11", minute="0"),
        "Schedule the day's content and win-back offers.",
    ),
    "menu_pricing": (
        crontab(day_of_week="1", hour="9", minute="0"),
        "Weekly menu engineering review (Monday morning).",
    ),
    "shift_scheduling": (
        crontab(day_of_week="3", hour="10", minute="0"),
        "Build next week's roster (Wednesday morning).",
    ),
    "bookkeeping": (
        crontab(hour="23", minute="30"),
        "Reconcile the day's takings and post journals.",
    ),
    "daily_performance": (
        crontab(hour="23", minute="45"),
        "End-of-day performance report.",
    ),
}

celery_app.conf.beat_schedule = {
    f"{agent}-scheduled": {
        "task": "restaurant_ai.worker.tasks.run_scheduled_agent",
        "schedule": schedule,
        "args": (agent,),
        "kwargs": {"reason": reason},
    }
    for agent, (schedule, reason) in SCHEDULE.items()
}

# Housekeeping alongside the agents.
celery_app.conf.beat_schedule["drain-outbox"] = {
    "task": "restaurant_ai.worker.tasks.drain_events",
    "schedule": crontab(minute="*/2"),
}
# The owner's brief goes out once the books are closed: Camelia reports at
# 23:45, the brief at 23:55, so its money section carries her verdict.
celery_app.conf.beat_schedule["daily-brief"] = {
    "task": "restaurant_ai.worker.tasks.send_daily_brief",
    "schedule": crontab(hour="23", minute="55"),
}
celery_app.conf.beat_schedule["expire-approvals"] = {
    "task": "restaurant_ai.worker.tasks.expire_stale_approvals",
    "schedule": crontab(hour="*/4", minute="5"),
}


def broker_available(timeout: float = 1.0) -> bool:
    """Whether Redis is reachable.

    The API uses this to decide between queuing work and running it inline, so
    a single-process development setup or the simulator exercises the same
    ingestion path without needing a worker.
    """
    if settings.celery_task_always_eager:
        return False
    try:
        import redis

        redis.from_url(
            settings.redis_url, socket_connect_timeout=timeout, socket_timeout=timeout
        ).ping()
        return True
    except Exception:
        return False
