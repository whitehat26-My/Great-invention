"""Campaigns, scheduled social posts and guest reviews."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from restaurant_ai.db.base import Base, Money, Timestamped, UUIDPk
from restaurant_ai.db.models.enums import ReviewSentiment


class Campaign(UUIDPk, Timestamped, Base):
    __tablename__ = "campaign"

    name: Mapped[str] = mapped_column(String(160), unique=True)
    objective: Mapped[str] = mapped_column(String(200))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    budget: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    posts: Mapped[list[SocialPost]] = relationship(back_populates="campaign")


class SocialPost(UUIDPk, Timestamped, Base):
    __tablename__ = "social_post"

    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaign.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40), index=True)
    body: Mapped[str] = mapped_column(Text)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_ref: Mapped[str | None] = mapped_column(String(80))
    featured_menu_item_id: Mapped[str | None] = mapped_column(ForeignKey("menu_item.id"))
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)

    campaign: Mapped[Campaign | None] = relationship(back_populates="posts")


class PromoOffer(UUIDPk, Timestamped, Base):
    """A targeted win-back or bundle offer issued to a guest segment."""

    __tablename__ = "promo_offer"

    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(300))
    discount_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date] = mapped_column(Date)
    segment: Mapped[str] = mapped_column(String(60), default="dormant")
    issued_count: Mapped[int] = mapped_column(Integer, default=0)
    redeemed_count: Mapped[int] = mapped_column(Integer, default=0)
    run_id: Mapped[str | None] = mapped_column(String(36))


class Review(UUIDPk, Timestamped, Base):
    """A guest review pulled from an external platform."""

    __tablename__ = "review"

    platform: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(80))
    author: Mapped[str] = mapped_column(String(160))
    rating: Mapped[int] = mapped_column(Integer, index=True)
    body: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    business_date: Mapped[date] = mapped_column(Date, index=True)
    sentiment: Mapped[ReviewSentiment | None] = mapped_column(
        Enum(ReviewSentiment, native_enum=False, length=16)
    )
    topics: Mapped[str | None] = mapped_column(String(300), doc="Comma-separated tags.")
    response_body: Mapped[str | None] = mapped_column(Text)
    response_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_escalated: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_review_platform_external"),
        Index("ix_review_rating_date", "rating", "business_date"),
    )
