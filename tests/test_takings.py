"""Recording a day's trading by hand.

The way in that needs no POS. What matters is that it is careful with money:
a name is matched rather than guessed, nothing is written before the owner has
seen it in ringgit, and what does get written counts as real trading.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from restaurant_ai import takings

pytestmark = pytest.mark.db


class TestReadingWhatSomebodyWrote:
    def test_a_number_in_front_or_behind_both_read(self):
        assert takings.split_entries("20 nasi lemak") == [("nasi lemak", 20)]
        assert takings.split_entries("nasi lemak 20") == [("nasi lemak", 20)]
        assert takings.split_entries("20 x nasi lemak") == [("nasi lemak", 20)]

    def test_commas_and_and_both_separate_dishes(self):
        assert takings.split_entries("20 rice, 5 tea and 3 roti") == [
            ("rice", 20),
            ("tea", 5),
            ("roti", 3),
        ]

    def test_a_dish_with_no_number_is_not_assumed_to_be_one(self):
        """One is a guess about money. It comes back as zero, to be asked about."""
        assert takings.split_entries("teh tarik") == [("teh tarik", 0)]


class TestMatchingADishOnTheRealMenu:
    @pytest.fixture
    def menu(self, db):
        from restaurant_ai.db.catalog_import import import_catalog

        import_catalog(
            db, "menu/the-great-invention-menu.xlsx", allow_uncosted=True, replace_menu=True
        )
        return db

    def test_an_exact_name_matches(self, menu):
        item, _ = takings.match_dish(menu, "Nasi Lemak Biasa")
        assert item is not None and item.sku == "MY-NLK-BIASA"

    def test_case_and_spacing_do_not_matter(self, menu):
        item, _ = takings.match_dish(menu, "  nasi   lemak biasa ")
        assert item is not None and item.sku == "MY-NLK-BIASA"

    def test_an_ambiguous_name_is_refused_with_its_candidates(self, menu):
        """Eight Nasi Lemaks. Closest would be a coin-flip about revenue."""
        item, candidates = takings.match_dish(menu, "nasi lemak")

        assert item is None
        assert len(candidates) > 1
        assert "Nasi Lemak Biasa" in candidates

    def test_a_dish_that_is_not_on_the_menu_matches_nothing(self, menu):
        item, candidates = takings.match_dish(menu, "pizza")
        assert item is None and candidates == []


class TestRecording:
    @pytest.fixture
    def menu(self, db):
        from restaurant_ai.db.catalog_import import import_catalog

        import_catalog(
            db, "menu/the-great-invention-menu.xlsx", allow_uncosted=True, replace_menu=True
        )
        return db

    def test_a_clean_day_is_written_with_its_money(self, menu):
        reading = takings.read(menu, "20 nasi lemak biasa, 35 teh tarik, 90 covers")
        assert reading.usable
        assert reading.covers == 90
        # 20 × 6.00 + 35 × 2.50
        assert reading.total == Decimal("207.50")

        written = takings.record(menu, reading)
        assert written["lines"] == 2
        assert written["covers"] == 90

    def test_what_is_written_counts_as_real_trading(self, menu):
        """The whole point: not the seed's H prefix, so it is not demo data."""
        from restaurant_ai import demo

        before = demo.real_orders(menu)
        takings.record(menu, takings.read(menu, "5 teh tarik"))

        assert demo.real_orders(menu) == before + 1
        assert not written_number_starts_with_h(menu)

    def test_a_reading_that_did_not_resolve_is_refused_outright(self, menu):
        """A partial write leaves the owner unsure which half went in."""
        reading = takings.read(menu, "12 nasi lemak, 5 teh tarik")
        assert not reading.usable

        with pytest.raises(ValueError, match="resolve"):
            takings.record(menu, reading)

    def test_two_entries_on_one_day_do_not_collide(self, menu):
        """Lunch logged, then dinner logged. Both must land."""
        first = takings.record(menu, takings.read(menu, "5 teh tarik"))
        second = takings.record(menu, takings.read(menu, "6 teh tarik"))
        assert first["order_number"] != second["order_number"]

    def test_the_lines_carry_the_menu_price(self, menu):
        from sqlalchemy import select

        from restaurant_ai.db.models import OrderHeader, OrderLine

        takings.record(menu, takings.read(menu, "3 roti kosong"))
        order = (
            menu.execute(
                select(OrderHeader)
                .where(OrderHeader.order_number.like("MAN-%"))
                .order_by(OrderHeader.created_at.desc())
            )
            .scalars()
            .first()
        )
        line = menu.execute(select(OrderLine).where(OrderLine.order_id == order.id)).scalars().one()
        assert line.unit_price == Decimal("1.80")
        assert line.line_total == Decimal("5.40")


def written_number_starts_with_h(session) -> bool:
    from sqlalchemy import select

    from restaurant_ai.db.models import OrderHeader

    numbers = session.execute(
        select(OrderHeader.order_number).where(OrderHeader.order_number.like("MAN-%"))
    ).scalars()
    return any(n.startswith("H") for n in numbers)
