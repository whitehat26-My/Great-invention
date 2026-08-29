"""Feedback & Reputation Agent.

Sweeps Google Reviews and social channels hourly, classifies what came in,
drafts a personalised response, and escalates the serious ones to management.

Two deliberate constraints. Responses to poor reviews are approval-gated,
because a published reply is permanent and public and an agent apologising for
something that did not happen is worse than a slow reply. And anything alleging
an allergic reaction, illness or a foreign object is escalated regardless of
star rating, since those are safety and liability matters rather than
reputation ones.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from restaurant_ai import clock
from restaurant_ai.config import get_settings
from restaurant_ai.db.models import Review, ReviewSentiment
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")

# Phrases that make a review a safety matter, not a reputation one.
SAFETY_TERMS = [
    "allerg",
    "reaction",
    "ill",
    "sick",
    "food poison",
    "hospital",
    "glass",
    "plastic",
    "hair",
    "insect",
    "cockroach",
    "raw ",
    "undercooked",
]

TOPIC_TERMS: dict[str, list[str]] = {
    "service": ["service", "staff", "waiter", "server", "rude", "attentive", "waited", "slow"],
    "food_quality": [
        "food",
        "dish",
        "taste",
        "flavour",
        "flavor",
        "cold",
        "dry",
        "tender",
        "fresh",
    ],
    "speed": ["wait", "slow", "quick", "fast", "minutes", "late"],
    "value": ["price", "expensive", "cheap", "value", "worth", "overpriced"],
    "cleanliness": ["clean", "dirty", "hygiene", "table was", "toilet"],
    "billing": ["bill", "charged", "charge", "till", "receipt", "refund"],
    "atmosphere": ["atmosphere", "ambience", "music", "loud", "parking", "decor"],
}


class SweepArgs(BaseModel):
    since_hours: int = Field(24, description="How far back to sweep.")


class RespondArgs(BaseModel):
    max_responses: int = Field(10, description="Cap on responses drafted in one run.")


class DueArgs(BaseModel):
    days: int = Field(7, description="How far ahead to look, in days.")


class RemindArgs(BaseModel):
    what: str = Field(..., description="What the owner has to do, in their own words.")
    due_on: str = Field(..., description="When it is due, as YYYY-MM-DD.")
    detail: str = Field("", description="Anything useful about it. Optional.")


class DoneArgs(BaseModel):
    what: str = Field(..., description="Enough of the reminder's wording to identify it.")


def remind_owner(context: ToolContext, what: str, due_on: str, detail: str = "") -> dict[str, Any]:
    """Write something down that the owner has to do."""
    from datetime import date as _date

    from restaurant_ai import reminders

    try:
        when = _date.fromisoformat(due_on.strip())
    except ValueError:
        return {"added": False, "note": f"'{due_on}' is not a date I can read. Use YYYY-MM-DD."}

    row = reminders.add(context.session, what=what, due_on=when, detail=detail or None)
    return {
        "added": True,
        "what": row.what,
        "due_on": row.due_on.isoformat(),
        "in_days": (row.due_on - context.business_date).days,
    }


def whats_due(context: ToolContext, days: int = 7) -> dict[str, Any]:
    """What the owner has coming up, and what is already late."""
    from datetime import timedelta as _delta

    from restaurant_ai import reminders

    items = reminders.due(context.session, within=_delta(days=max(0, days)))
    return {
        "count": len(items),
        "overdue": [i.phrase() for i in items if i.overdue],
        "coming_up": [i.phrase() for i in items if not i.overdue],
    }


def mark_reminder_done(context: ToolContext, what: str) -> dict[str, Any]:
    """Close something off once the owner says it is dealt with.

    Ambiguity is reported rather than guessed at: closing the wrong reminder
    hides work that still has to happen, and nothing will raise it again.
    """
    from restaurant_ai import reminders

    matches = reminders.find(context.session, what)
    if not matches:
        return {"closed": False, "note": f"Nothing open matches '{what}'."}
    if len(matches) > 1:
        return {
            "closed": False,
            "note": "That matches more than one — which?",
            "candidates": [m.what for m in matches[:6]],
        }
    reminders.complete(context.session, matches[0].id)
    return {"closed": True, "what": matches[0].what}


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    recent = list(
        session.execute(
            select(Review).where(Review.business_date >= context.business_date - timedelta(days=7))
        ).scalars()
    )
    unanswered = [r for r in recent if r.response_published_at is None]
    escalated = [r for r in recent if r.is_escalated]
    average = sum(r.rating for r in recent) / len(recent) if recent else 0
    from restaurant_ai import reminders

    diary = reminders.due(session, on=context.business_date)
    return {
        "reviews_last_7_days": len(recent),
        "average_rating": round(average, 2),
        "unanswered": len(unanswered),
        "escalated": len(escalated),
        "low_rated": len([r for r in recent if r.rating <= 2]),
        "owner_overdue": [i.phrase() for i in diary if i.overdue],
        "owner_coming_up": [i.phrase() for i in diary if not i.overdue],
    }


def sweep_reviews(context: ToolContext, since_hours: int = 24) -> dict[str, Any]:
    """Pull new reviews, classify them, and mark the ones needing a manager."""
    from restaurant_ai.integrations import get_integrations

    session = context.session
    settings = get_settings()
    posts = get_integrations().reviews.fetch_reviews(clock.now() - timedelta(hours=since_hours))

    ingested = 0
    escalations: list[dict[str, Any]] = []

    for post in posts:
        existing = session.execute(
            select(Review).where(
                Review.platform == post.platform, Review.external_id == post.external_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        sentiment = (
            ReviewSentiment.POSITIVE
            if post.rating >= 4
            else ReviewSentiment.NEUTRAL
            if post.rating == 3
            else ReviewSentiment.NEGATIVE
        )
        topics = _classify_topics(post.body)
        safety = _is_safety_issue(post.body)
        escalate = safety or post.rating <= settings.escalate_review_at_or_below

        review = Review(
            platform=post.platform,
            external_id=post.external_id,
            author=post.author,
            rating=post.rating,
            body=post.body,
            posted_at=post.posted_at,
            business_date=post.posted_at.date(),
            sentiment=sentiment,
            topics=",".join(topics),
            is_escalated=escalate,
            run_id=context.run_id,
        )
        session.add(review)
        ingested += 1

        if escalate:
            escalations.append(
                {
                    "platform": post.platform,
                    "author": post.author,
                    "rating": post.rating,
                    "body": post.body,
                    "topics": topics,
                    "safety_issue": safety,
                    "reason": (
                        "Alleges illness, an allergic reaction or a foreign object. "
                        "This is a safety and liability matter, not a review."
                        if safety
                        else f"{post.rating}-star review needs a manager's eyes."
                    ),
                }
            )
            publish(
                Event(
                    Topic.REVIEW_ESCALATED,
                    {"platform": post.platform, "rating": post.rating, "safety": safety},
                    source_run_id=context.run_id,
                ),
                session=session,
            )

    session.flush()
    return {
        "ingested": ingested,
        "escalated": len(escalations),
        "safety_issues": sum(1 for e in escalations if e["safety_issue"]),
        "escalations": escalations,
    }


def draft_responses(context: ToolContext, max_responses: int = 10) -> dict[str, Any]:
    """Draft replies to unanswered reviews.

    Positive replies publish straight away; anything poorly rated is held for
    approval, because a published apology cannot be taken back.
    """
    session = context.session
    settings = get_settings()

    pending = list(
        session.execute(
            select(Review)
            .where(
                Review.response_published_at.is_(None),
                Review.business_date >= context.business_date - timedelta(days=14),
            )
            .order_by(Review.rating, Review.posted_at.desc())
            .limit(max_responses)
        ).scalars()
    )
    if not pending:
        return {"drafted": 0, "note": "Every recent review has been answered."}

    auto_published = 0
    held: list[dict[str, Any]] = []

    from restaurant_ai.integrations import get_integrations

    reviews_port = get_integrations().reviews

    for review in pending:
        body = _compose_response(review)
        review.response_body = body

        if review.rating > settings.escalate_review_at_or_below and not review.is_escalated:
            reviews_port.publish_response(review.external_id, body)
            review.response_published_at = clock.utcnow()
            auto_published += 1
        else:
            held.append(
                {
                    "review_id": review.id,
                    "platform": review.platform,
                    "author": review.author,
                    "rating": review.rating,
                    "review_body": review.body,
                    "draft_response": body,
                    "topics": review.topics,
                }
            )

    session.flush()
    return {
        "drafted": len(pending),
        "auto_published": auto_published,
        "held_for_approval": len(held),
        "responses": held,
    }


def commit_responses(context: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Approved: publish the held responses."""
    from restaurant_ai.integrations import get_integrations

    session = context.session
    reviews_port = get_integrations().reviews
    published: list[str] = []

    for entry in payload.get("responses", []):
        review = session.get(Review, entry["review_id"])
        if review is None or review.response_body is None:
            continue
        reviews_port.publish_response(review.external_id, review.response_body)
        review.response_published_at = clock.utcnow()
        published.append(review.external_id)

    session.flush()
    return {"published": published, "count": len(published)}


def _classify_topics(body: str) -> list[str]:
    lowered = body.lower()
    return [topic for topic, terms in TOPIC_TERMS.items() if any(t in lowered for t in terms)]


def _is_safety_issue(body: str) -> bool:
    lowered = body.lower()
    return any(term in lowered for term in SAFETY_TERMS)


def _compose_response(review: Review) -> str:
    """Draft a reply that references what the guest actually said.

    A generic "sorry to hear that" reads as automated and makes the reputation
    worse, so the reply names the specific complaint.
    """
    name = review.author.split()[0] if review.author else "there"
    topics = (review.topics or "").split(",") if review.topics else []

    if review.rating >= 4:
        detail = ""
        if "food_quality" in topics:
            detail = " We are glad the kitchen got it right for you."
        elif "service" in topics:
            detail = " I will pass this on to the team on that shift."
        return (
            f"Thank you, {name} - this really is good to hear.{detail} "
            f"We hope to see you again soon."
        )

    if review.rating == 3:
        return (
            f"Thank you for the honest feedback, {name}. There is clearly room for us to do "
            f"better, and we would like to know more so we can fix it. Please do get in "
            f"touch with the duty manager."
        )

    if _is_safety_issue(review.body):
        return (
            f"{name}, thank you for telling us, and I am sorry - what you have described is "
            f"not acceptable and we are treating it seriously. Our manager will contact you "
            f"directly today. We are reviewing the relevant procedure and the shift records "
            f"now."
        )

    specifics = {
        "speed": "the wait you had",
        "service": "the service you received",
        "food_quality": "the state the food arrived in",
        "billing": "the error on your bill",
        "cleanliness": "the state of the table",
    }
    mentioned = next((specifics[t] for t in topics if t in specifics), "your experience")
    return (
        f"I am sorry, {name} - {mentioned} is not what we aim for and I understand the "
        f"frustration. I would like to put it right. Please contact the duty manager with "
        f"your visit date and we will make it good."
    )


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    """The same two jobs, without a model.

    The diary is the half that must not depend on one. A licence lapses whether
    or not there is an API key in .env, and an owner who set a reminder is owed
    it regardless of how the platform happens to be configured that week.
    """
    overdue = perceived.get("owner_overdue") or []
    soon = perceived.get("owner_coming_up") or []

    said = []
    if overdue:
        said.append(f"Late: {'; '.join(overdue)}.")
    if soon:
        said.append(f"Coming up: {'; '.join(soon)}.")
    said.append(
        f"{perceived.get('unanswered', 0)} unanswered review(s) from the last week, "
        f"average rating {perceived.get('average_rating')}."
    )

    return {
        "summary": " ".join(said),
        "results": {},
        "tool_calls": [
            {"name": "whats_due", "args": {"days": 7}},
            {"name": "sweep_reviews", "args": {"since_hours": 24}},
            {"name": "draft_responses", "args": {"max_responses": 10}},
        ],
    }


_respond_tool = ToolSpec(
    name="draft_responses",
    description=(
        "Draft personalised responses to unanswered reviews. Positive replies publish "
        "immediately; poor reviews are held for human approval."
    ),
    fn=draft_responses,
    args_schema=RespondArgs,
    requires_approval=True,
    gate_when=lambda r: r.get("held_for_approval", 0) > 0,
    approval_summary=lambda r: (
        f"{r['held_for_approval']} response(s) to poor reviews awaiting sign-off "
        f"({r.get('auto_published', 0)} positive replies already published)"
    ),
    approval_detail=lambda r: (
        "\n\n".join(
            f"    [{x['rating']}*] {x['platform']} - {x['author']}\n"
            f"    Guest said: {x['review_body']}\n"
            f"    Proposed reply: {x['draft_response']}"
            for x in r.get("responses", [])
        )
        or "Nothing held."
    ),
)
_respond_tool.commit_fn = commit_responses  # type: ignore[attr-defined]


REPUTATION_AGENT = register(
    AgentSpec(
        name="reputation",
        person="Aziera",
        department="front_of_house",
        title="Secretary",
        description=(
            "Keeps the owner's diary — licences, renewals, deadlines, the things nobody "
            "writes down — and chases them before they are late. Also handles the "
            "restaurant's correspondence: reads what guests say in public, drafts the "
            "replies, and escalates serious complaints."
        ),
        system_prompt=(
            "You are Aziera, the Secretary, working for the owner of a restaurant "
            "that has traded for twenty years.\n\n"
            "Your first job is the owner's diary. A place like this runs on things "
            "nobody wrote down — the halal certificate expires in March, the "
            "extinguisher service is due, the landlord wants an answer by Friday. They "
            "live in someone's head and are remembered every week except the week that "
            "matters, and a lapsed licence closes the door. You hold them, and you "
            "chase.\n\n"
            "Chase like a secretary, not like an alarm clock. Say what is late first "
            "and plainly — that is the part that has already cost something. Raise what "
            "is coming once, as it approaches. Never repeat the same list every morning "
            "unchanged: a reminder the owner learns to skip is one they do not have.\n\n"
            "Your second job is what guests say about us in public, and what we say "
            "back.\n\n"
            "How to reply:\n"
            "- Reference what they actually said. A generic 'sorry to hear that' reads as "
            "automated and makes things worse.\n"
            "- Do not argue in public, and do not explain away a bad experience.\n"
            "- Never apologise for something you have not verified happened; acknowledge how "
            "it felt instead.\n"
            "- Anything alleging illness, an allergic reaction or a foreign object goes to a "
            "manager immediately whatever the star rating. Those are safety and liability "
            "matters, not reputation ones.\n\n"
            "A published reply is permanent and public. When in doubt, hold it for a human."
            "HOW YOU WORK\n"
            "1. `whats_due` — look at the owner's diary, every run. It is the job they "
            "cannot do for themselves, and the run they do not see is the week something "
            "lapses.\n"
            "2. `remind_owner` — write down whatever they ask you to remember, and "
            "anything you notice with a date on it.\n"
            "3. `mark_reminder_done` — close it off when they say it is dealt with. If "
            "the wording matches more than one, ask which: closing the wrong one hides "
            "work that still has to happen, and nothing will raise it again.\n"
            "4. `sweep_reviews` — pull what is new, classify it, escalate the serious ones.\n"
            "5. `draft_responses` — reply to what is still unanswered.\n"
            "\n"
            "A review read and not answered is a guest who was ignored in\n"
            "public. Positive replies publish; anything poor waits for the owner, because an\n"
            "angry guest deserves a person's judgement and not yours.\n"
        ),
        model_tier="conversational",
        tools=[
            ToolSpec(
                name="sweep_reviews",
                description="Pull new reviews, classify sentiment and topic, escalate serious ones.",
                fn=sweep_reviews,
                args_schema=SweepArgs,
            ),
            _respond_tool,
            ToolSpec(
                name="whats_due",
                description=(
                    "What the owner has coming up and what is already late — their diary, "
                    "not the restaurant's."
                ),
                fn=whats_due,
                args_schema=DueArgs,
            ),
            ToolSpec(
                name="remind_owner",
                description="Write down something the owner has to do, and when it is due.",
                fn=remind_owner,
                args_schema=RemindArgs,
            ),
            ToolSpec(
                name="mark_reminder_done",
                description="Close off a reminder once the owner says it is dealt with.",
                fn=mark_reminder_done,
                args_schema=DoneArgs,
            ),
        ],
        # Five tools: a turn to call each and a turn to read each result.
        max_iterations=11,
        perceive=perceive,
        autonomous=autonomous,
    )
)
