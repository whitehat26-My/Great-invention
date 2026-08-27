"""Social Media & Content Agent.

Writes promotional copy, schedules it, and builds win-back offers for diners who
have stopped coming.

Content is anchored to what the restaurant actually wants to move: the pricing
agent's menu classification decides what gets featured, so Puzzles (high margin,
low awareness) get the promotion they need rather than Stars getting more
attention they do not.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from restaurant_ai import clock
from restaurant_ai.config import get_settings
from restaurant_ai.db.models import Guest, MenuClass, MenuItem, PromoOffer, SocialPost
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")
PLATFORMS = ["instagram", "facebook", "tiktok"]

# Copy templates per quadrant. The angle differs because the job differs:
# a Puzzle needs introducing, a Star needs reinforcing.
ANGLES: dict[str, list[str]] = {
    "puzzle": [
        "Ask us what to order and we will point you here. {name} - {description} {price}",
        "The one the regulars order and nobody else has found yet. {name}. {price}",
        "Quietly the best thing on our menu. {name} - {description}",
    ],
    "star": [
        "The one you came for. {name}, made the way it should be. {price}",
        "{name}. Still the reason people book. {price}",
    ],
    "dog": [
        "This week only: {name} paired and priced to move. {price}",
    ],
    "default": [
        "On the pass today: {name} - {description} {price}",
        "{name}. {description}",
    ],
}


class ContentArgs(BaseModel):
    posts: int = Field(3, description="How many posts to schedule.")
    days_ahead: int = Field(3, description="Spread posts across this many days.")


class ReengageArgs(BaseModel):
    dormant_days: int = Field(45, description="Days since last visit to count as dormant.")
    discount_pct: str = Field("0.15", description="Win-back discount.")


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    settings = get_settings()
    cutoff = context.business_date - timedelta(days=settings.reengagement_dormant_days)

    dormant = session.execute(
        select(func.count(Guest.id)).where(
            Guest.last_visit_on.isnot(None),
            Guest.last_visit_on < cutoff,
            Guest.marketing_opt_in.is_(True),
        )
    ).scalar_one()

    scheduled = session.execute(
        select(func.count(SocialPost.id)).where(
            SocialPost.scheduled_for >= clock.now(), SocialPost.published_at.is_(None)
        )
    ).scalar_one()

    return {
        "dormant_guests": int(dormant),
        "posts_already_scheduled": int(scheduled),
        "dormant_threshold_days": settings.reengagement_dormant_days,
    }


def schedule_content(context: ToolContext, posts: int = 3, days_ahead: int = 3) -> dict[str, Any]:
    """Draft promotional posts featuring what needs promoting. Approval-gated.

    Writes the posts and stops. Nothing reaches a platform until a human
    approves: the rows land with no ``external_ref``, which is what makes a
    drafted post distinguishable from a published one without a status column.
    """
    session = context.session

    # Puzzles first: high margin, low awareness is exactly what marketing fixes.
    candidates = list(
        session.execute(
            select(MenuItem)
            .where(MenuItem.is_active, MenuItem.menu_class == MenuClass.PUZZLE)
            .limit(posts)
        ).scalars()
    )
    if len(candidates) < posts:
        candidates += list(
            session.execute(
                select(MenuItem)
                .where(
                    MenuItem.is_active,
                    MenuItem.menu_class == MenuClass.STAR,
                    MenuItem.id.notin_([c.id for c in candidates] or [""]),
                )
                .limit(posts - len(candidates))
            ).scalars()
        )
    if not candidates:
        candidates = list(
            session.execute(select(MenuItem).where(MenuItem.is_active).limit(posts)).scalars()
        )

    scheduled: list[dict[str, Any]] = []
    for index, item in enumerate(candidates[:posts]):
        angle_key = item.menu_class.value if item.menu_class else "default"
        templates = ANGLES.get(angle_key, ANGLES["default"])
        body = templates[index % len(templates)].format(
            name=item.name,
            description=(item.description or "").rstrip("."),
            price=f"RM{item.price}",
        )
        platform = PLATFORMS[index % len(PLATFORMS)]
        when = clock.now() + timedelta(days=(index % max(days_ahead, 1)), hours=11)

        post = SocialPost(
            platform=platform,
            body=body,
            scheduled_for=when,
            featured_menu_item_id=item.id,
            run_id=context.run_id,
        )
        session.add(post)
        session.flush()

        scheduled.append(
            {
                "post_id": post.id,
                "platform": platform,
                "featuring": item.name,
                "menu_class": angle_key,
                "scheduled_for": when.isoformat(),
                "body": body,
                "why": (
                    "High margin but low awareness - promotion is the right lever."
                    if angle_key == "puzzle"
                    else "Proven seller; reinforces what people already come for."
                    if angle_key == "star"
                    else "Needs volume."
                ),
            }
        )

    session.flush()
    return {"scheduled": len(scheduled), "posts": scheduled}


def publish_content(context: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Send the approved posts to the platforms. Runs only after a human says yes."""
    from restaurant_ai.integrations import get_integrations
    from restaurant_ai.integrations.base import ScheduledPost

    session = context.session
    social = get_integrations().social
    published: list[str] = []

    for entry in payload.get("posts") or []:
        post = session.get(SocialPost, entry.get("post_id"))
        if post is None or post.external_ref:
            continue  # already out, or gone; never publish the same post twice
        post.external_ref = social.schedule_post(
            ScheduledPost(platform=post.platform, body=post.body, scheduled_for=post.scheduled_for)
        )
        published.append(post.id)

    session.flush()
    return {
        "published": len(published),
        "post_ids": published,
        "approved_by": payload.get("approved_by"),
    }


def build_reengagement(
    context: ToolContext, dormant_days: int = 45, discount_pct: str = "0.15"
) -> dict[str, Any]:
    """Draft a win-back offer for guests who have stopped visiting. Approval-gated.

    The offer is written with ``issued_count`` at zero — it exists, and it has
    reached nobody. A discount the restaurant honours for thirty days is a
    commercial commitment of the same kind as a price change, and Irma's price
    moves already stop for a human.
    """
    session = context.session
    cutoff = context.business_date - timedelta(days=dormant_days)

    dormant = list(
        session.execute(
            select(Guest).where(
                Guest.last_visit_on.isnot(None),
                Guest.last_visit_on < cutoff,
                Guest.marketing_opt_in.is_(True),
            )
        ).scalars()
    )
    if not dormant:
        return {"issued": 0, "note": f"No opted-in guests dormant beyond {dormant_days} days."}

    code = f"WELCOME{context.business_date.strftime('%y%m')}"
    existing = session.execute(
        select(PromoOffer).where(PromoOffer.code == code)
    ).scalar_one_or_none()
    if existing is not None:
        return {
            "issued": 0,
            "code": code,
            "note": "This month's win-back offer already exists.",
        }

    offer = PromoOffer(
        code=code,
        description=(
            f"{Decimal(discount_pct) * 100:.0f}% off for guests who have not visited in "
            f"{dormant_days} days"
        ),
        discount_pct=Decimal(discount_pct),
        valid_from=context.business_date,
        valid_to=context.business_date + timedelta(days=30),
        segment="dormant",
        issued_count=0,
        run_id=context.run_id,
    )
    session.add(offer)
    session.flush()

    lifetime_value = sum((g.lifetime_value for g in dormant), ZERO)
    return {
        "offer_id": offer.id,
        "issued": len(dormant),
        "code": code,
        "discount_pct": discount_pct,
        "valid_to": offer.valid_to.isoformat(),
        "segment_lifetime_value": str(lifetime_value),
        "note": (
            f"{len(dormant)} opted-in guest(s) have not visited since {cutoff}. "
            f"Offer {code} drafted, valid 30 days once approved."
        ),
    }


def issue_offer(context: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Put the approved offer in front of the segment it was drafted for."""
    session = context.session
    offer = session.get(PromoOffer, payload.get("offer_id"))
    if offer is None:
        return {"issued": 0, "note": "The drafted offer no longer exists."}

    offer.issued_count = int(payload.get("issued") or 0)
    session.flush()
    return {
        "issued": offer.issued_count,
        "code": offer.code,
        "approved_by": payload.get("approved_by"),
    }


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = [
        {"name": "schedule_content", "args": {"posts": 3, "days_ahead": 3}}
    ]
    if perceived.get("dormant_guests", 0) > 0:
        calls.append(
            {
                "name": "build_reengagement",
                "args": {
                    "dormant_days": perceived.get("dormant_threshold_days", 45),
                    "discount_pct": "0.15",
                },
            }
        )
    return {
        "summary": (
            f"Scheduling content and, for {perceived.get('dormant_guests', 0)} dormant "
            f"guest(s), a win-back offer."
        ),
        "results": {},
        "tool_calls": calls,
    }


_content_tool = ToolSpec(
    name="schedule_content",
    description=(
        "Draft promotional posts featuring what needs promoting. Requires human "
        "approval before anything is published."
    ),
    fn=schedule_content,
    args_schema=ContentArgs,
    requires_approval=True,
    gate_when=lambda r: r.get("scheduled", 0) > 0,
    approval_summary=lambda r: f"{r['scheduled']} post(s) to publish",
    approval_detail=lambda r: "\n\n".join(
        f"{p['platform']} — {p['scheduled_for'][:16]}\n{p['body']}\n({p['why']})"
        for p in r.get("posts", [])
    ),
)
_content_tool.commit_fn = publish_content  # type: ignore[attr-defined]


_reengagement_tool = ToolSpec(
    name="build_reengagement",
    description=(
        "Draft a win-back offer for guests who have stopped visiting. Requires "
        "human approval before it is issued to anyone."
    ),
    fn=build_reengagement,
    args_schema=ReengageArgs,
    requires_approval=True,
    gate_when=lambda r: r.get("issued", 0) > 0,
    # The exposure is the discount against what that segment is worth, which is
    # the number that decides whether this is worth doing.
    approval_value=lambda r: (
        Decimal(str(r.get("segment_lifetime_value", "0")))
        * Decimal(str(r.get("discount_pct", "0")))
    ),
    approval_summary=lambda r: (
        f"{r['code']}: {Decimal(str(r['discount_pct'])) * 100:.0f}% off to "
        f"{r['issued']} dormant guest(s)"
    ),
    approval_detail=lambda r: (
        f"{r['note']}\n"
        f"Segment lifetime value {r['segment_lifetime_value']}; valid to {r['valid_to']}."
    ),
)
_reengagement_tool.commit_fn = issue_offer  # type: ignore[attr-defined]


SOCIAL_CONTENT_AGENT = register(
    AgentSpec(
        name="social_content",
        person="Franky",
        department="marketing",
        title="Social Media & Content Agent",
        description=(
            "Generates promotional copy, schedules posts, and triggers automated "
            "re-engagement promos for past diners."
        ),
        system_prompt=(
            "You are the Social Media and Content Agent for a restaurant.\n\n"
            "Write like the restaurant, not like an advert. Short, specific, and about the "
            "food. No exclamation marks, no stock phrases, no emoji walls.\n\n"
            "Feature what the business actually needs to move. A dish with a strong margin "
            "that nobody orders is the one worth a post; the bestseller does not need your "
            "help. Name the dish, say what is in it, give the price.\n\n"
            "For win-back offers, be direct about why you are writing and make the offer worth "
            "opening. Only ever contact guests who opted in."
        ),
        model_tier="conversational",
        tools=[
            _content_tool,
            _reengagement_tool,
        ],
        perceive=perceive,
        autonomous=autonomous,
    )
)
