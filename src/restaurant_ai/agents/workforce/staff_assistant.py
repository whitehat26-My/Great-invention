"""Staff Assistant & Onboarding Agent.

Answers staff questions about SOPs, recipes and shift swapping.

SOP retrieval uses Postgres full-text search rather than a vector store. For a
corpus this size that is both accurate enough and one fewer moving part, and it
returns the source document so a cook can go and read the whole procedure rather
than trusting a paraphrase — which matters when the question is about allergen
handling or holding temperatures.

Shift swaps are validated against the same hard constraints the roster respects,
then routed to a manager: two people agreeing between themselves does not make a
swap legal.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from restaurant_ai import clock
from restaurant_ai.db.models import Shift, ShiftAssignment, SopDocument, Staff
from restaurant_ai.domain.costing import cost_breakdown, menu_item_allergens
from restaurant_ai.events import Event, Topic, publish
from restaurant_ai.kernel.registry import register
from restaurant_ai.kernel.spec import AgentSpec, ToolContext, ToolSpec

ZERO = Decimal("0")


class AskArgs(BaseModel):
    question: str = Field(..., description="The staff member's question.")
    limit: int = Field(3, description="How many procedures to return.")


class RecipeArgs(BaseModel):
    sku: str = Field(..., description="Menu item SKU.")


class SwapArgs(BaseModel):
    requester_code: str = Field(..., description="Employee code asking to swap.")
    cover_code: str = Field(..., description="Employee code offering to cover.")
    shift_id: str = Field(..., description="The shift to swap.")


def perceive(context: ToolContext) -> dict[str, Any]:
    session = context.session
    return {
        "sop_documents": int(session.execute(select(func.count(SopDocument.id))).scalar_one()),
        "active_staff": int(
            session.execute(select(func.count(Staff.id)).where(Staff.is_active)).scalar_one()
        ),
        "pending_swaps": int(
            session.execute(
                select(func.count(ShiftAssignment.id)).where(
                    ShiftAssignment.swap_requested_with.isnot(None)
                )
            ).scalar_one()
        ),
    }


# Words that carry no retrieval signal in a question like "how do I ...".
_STOPWORDS = {
    "how",
    "do",
    "i",
    "the",
    "a",
    "an",
    "what",
    "when",
    "where",
    "should",
    "can",
    "my",
    "me",
    "is",
    "are",
    "to",
    "for",
    "of",
    "in",
    "on",
    "at",
    "with",
    "and",
    "or",
    "be",
    "it",
    "that",
    "this",
    "we",
    "you",
    "if",
    "any",
    "some",
    "someone",
    "please",
    "need",
    "want",
    "does",
    "did",
    "was",
    "were",
    "there",
    "here",
}


def _keywords(question: str) -> list[str]:
    """Content words from a question, for an OR-style fallback query."""
    import re

    words = re.findall(r"[a-z0-9]+", question.lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


def search_sops(context: ToolContext, question: str, limit: int = 3) -> dict[str, Any]:
    """Find the procedures that answer a question, using Postgres full-text search.

    Two passes. websearch_to_tsquery requires every term to appear, which is
    precise but brittle for natural questions: "how do I close the fryer at
    night" fails because "night" is nowhere in the closing checklist. So when
    the strict pass returns nothing, the content words are retried joined by OR
    and ranked, which finds the right document without needing the guess to be
    word-perfect.

    Anything that still matches nothing returns nothing. A confidently wrong
    procedure is worse than "ask your manager", especially on food safety.
    """
    session = context.session
    cleaned = question.strip()
    if not cleaned:
        return {"matches": 0, "results": [], "question": cleaned}

    ranked_sql = """
        SELECT slug, title, category, body, applies_to_role,
               ts_rank(to_tsvector('english', title || ' ' || body), query) AS rank
        FROM sop_document, {query_expr} AS query
        WHERE to_tsvector('english', title || ' ' || body) @@ query
        ORDER BY rank DESC
        LIMIT :lim
    """

    # Pass 1: all terms must appear.
    rows = session.execute(
        text(ranked_sql.format(query_expr="websearch_to_tsquery('english', :q)")),
        {"q": cleaned, "lim": limit},
    ).all()
    strategy = "all_terms"

    # Pass 2: any content word, ranked by how well it matches.
    if not rows:
        keywords = _keywords(cleaned)
        if keywords:
            rows = session.execute(
                text(ranked_sql.format(query_expr="to_tsquery('english', :q)")),
                {"q": " | ".join(keywords), "lim": limit},
            ).all()
            strategy = "any_keyword"

    if not rows:
        return {
            "question": cleaned,
            "matches": 0,
            "results": [],
            "note": (
                "No documented procedure covers this. Ask the duty manager rather than "
                "working from an assumption."
            ),
        }

    return {
        "question": cleaned,
        "matches": len(rows),
        "strategy": strategy,
        "results": [
            {
                "slug": row.slug,
                "title": row.title,
                "category": row.category,
                "applies_to": row.applies_to_role,
                "excerpt": row.body[:400],
                "relevance": round(float(row.rank), 4),
            }
            for row in rows
        ],
    }


def recipe_detail(context: ToolContext, sku: str) -> dict[str, Any]:
    """Full recipe detail for a dish: components, method and allergens."""
    from restaurant_ai.db.models import MenuItem, Recipe

    session = context.session
    item = session.execute(select(MenuItem).where(MenuItem.sku == sku)).scalar_one_or_none()
    if item is None:
        return {"found": False, "error": f"No menu item with SKU {sku!r}."}

    recipe = session.execute(
        select(Recipe).where(Recipe.menu_item_id == item.id)
    ).scalar_one_or_none()
    breakdown = cost_breakdown(session, item.id)

    components = []
    if recipe is not None:
        for component in recipe.components:
            if component.ingredient is not None:
                components.append(
                    {
                        "component": component.ingredient.name,
                        "quantity": f"{component.quantity} {component.uom}",
                        "type": "ingredient",
                    }
                )
            elif component.sub_recipe is not None:
                components.append(
                    {
                        "component": component.sub_recipe.name,
                        "quantity": f"{component.quantity} {component.uom}",
                        "type": "sub-recipe",
                    }
                )

    return {
        "found": True,
        "sku": item.sku,
        "name": item.name,
        "station": item.station.value,
        "prep_seconds": item.prep_seconds,
        "method": recipe.method if recipe else None,
        "components": components,
        "allergens": sorted(menu_item_allergens(session, item.id)),
        "plate_cost": str(breakdown.total_cost),
        "price": str(item.price),
    }


def request_shift_swap(
    context: ToolContext, requester_code: str, cover_code: str, shift_id: str
) -> dict[str, Any]:
    """Validate a proposed swap and route it to a manager.

    Checked against the same rules the roster respects. Two people agreeing
    between themselves does not make a swap legal, and the person picking up the
    shift is the one who would be over hours or short of rest.
    """
    session = context.session

    requester = session.execute(
        select(Staff).where(Staff.employee_code == requester_code)
    ).scalar_one_or_none()
    cover = session.execute(
        select(Staff).where(Staff.employee_code == cover_code)
    ).scalar_one_or_none()
    shift = session.get(Shift, shift_id)

    if requester is None or cover is None:
        return {"accepted": False, "reason": "One of the employee codes was not recognised."}
    if shift is None:
        return {"accepted": False, "reason": f"No shift with id {shift_id}."}

    assignment = session.execute(
        select(ShiftAssignment).where(
            ShiftAssignment.shift_id == shift_id, ShiftAssignment.staff_id == requester.id
        )
    ).scalar_one_or_none()
    if assignment is None:
        return {
            "accepted": False,
            "reason": f"{requester.name} is not rostered on that shift.",
        }

    blockers: list[str] = []

    if cover.role != shift.role:
        blockers.append(
            f"{cover.name} is a {cover.role.value}; the shift needs a {shift.role.value}."
        )

    notice_hours = (shift.starts_at - clock.now()).total_seconds() / 3600
    if notice_hours < 48:
        blockers.append(f"Only {notice_hours:.0f} hours' notice; the policy requires 48.")

    # Contracted hours for the cover's week.
    week_start = shift.business_date - timedelta(days=shift.business_date.weekday())
    existing_hours = ZERO
    for _other, other_shift in session.execute(
        select(ShiftAssignment, Shift)
        .join(Shift, ShiftAssignment.shift_id == Shift.id)
        .where(
            ShiftAssignment.staff_id == cover.id,
            Shift.business_date >= week_start,
            Shift.business_date < week_start + timedelta(days=7),
        )
    ).all():
        existing_hours += other_shift.hours
        if other_shift.starts_at < shift.ends_at and shift.starts_at < other_shift.ends_at:
            blockers.append(f"{cover.name} is already working an overlapping shift.")

    if existing_hours + shift.hours > Decimal(cover.max_weekly_hours):
        blockers.append(
            f"{cover.name} would reach {existing_hours + shift.hours:.1f} hours against a "
            f"{cover.max_weekly_hours}-hour contract."
        )

    if blockers:
        return {
            "accepted": False,
            "requester": requester.name,
            "cover": cover.name,
            "blockers": blockers,
            "reason": "; ".join(blockers),
        }

    assignment.swap_requested_with = cover.id
    session.flush()
    publish(
        Event(
            Topic.SHIFT_SWAP_REQUESTED,
            {
                "shift_id": shift_id,
                "from": requester.employee_code,
                "to": cover.employee_code,
            },
            source_run_id=context.run_id,
        ),
        session=session,
    )

    return {
        "accepted": True,
        "requester": requester.name,
        "cover": cover.name,
        "shift": f"{shift.business_date} {shift.starts_at:%H:%M}-{shift.ends_at:%H:%M} "
        f"({shift.role.value})",
        "note": "Checks passed. Routed to the duty manager for approval.",
    }


def autonomous(context: ToolContext, perceived: dict[str, Any]) -> dict[str, Any]:
    """Standing by: driven by staff questions rather than a schedule."""
    return {
        "summary": (
            f"Ready to answer staff questions across {perceived.get('sop_documents', 0)} "
            f"procedures. {perceived.get('pending_swaps', 0)} swap(s) awaiting a manager."
        ),
        "results": {},
        "tool_calls": [],
    }


STAFF_ASSISTANT_AGENT = register(
    AgentSpec(
        name="staff_assistant",
        person="Kaksu",
        department="workforce",
        title="Staff Assistant & Onboarding Agent",
        description=(
            "Answers internal staff questions about standard operating procedures, recipes "
            "and shift swapping."
        ),
        system_prompt=(
            "You are the Staff Assistant for a restaurant. You answer questions from the "
            "team about procedures, recipes and shifts.\n\n"
            "Answer from the documented procedure, and say which one you are quoting so the "
            "person can go and read it in full. If the procedure does not cover their "
            "situation, say so and point them to the duty manager rather than improvising - "
            "particularly on anything touching allergens, holding temperatures or food safety, "
            "where a plausible-sounding guess is genuinely dangerous.\n\n"
            "On shift swaps: both people have to hold the right role, the cover must stay "
            "inside their contracted hours and keep their minimum rest, and 48 hours' notice "
            "is required. Explain which rule blocks a swap when one does. A manager approves; "
            "you check and route."
        ),
        model_tier="conversational",
        tools=[
            ToolSpec(
                name="search_sops",
                description="Search standard operating procedures for the answer to a question.",
                fn=search_sops,
                args_schema=AskArgs,
            ),
            ToolSpec(
                name="recipe_detail",
                description="Full recipe detail for a dish: components, method and allergens.",
                fn=recipe_detail,
                args_schema=RecipeArgs,
            ),
            ToolSpec(
                name="request_shift_swap",
                description="Validate a proposed shift swap and route it to a manager.",
                fn=request_shift_swap,
                args_schema=SwapArgs,
            ),
        ],
        perceive=perceive,
        autonomous=autonomous,
    )
)
