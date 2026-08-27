from decimal import Decimal

import pytest

from restaurant_ai.domain.units import UomError, compatible, convert, dimension_of


class TestConvert:
    @pytest.mark.parametrize(
        ("qty", "src", "dst", "expected"),
        [
            ("2", "kg", "g", "2000"),
            ("1500", "g", "kg", "1.5"),
            ("1", "l", "ml", "1000"),
            ("250", "ml", "l", "0.25"),
            ("3", "ea", "ea", "3"),
            ("500", "mg", "g", "0.5"),
        ],
    )
    def test_within_dimension(self, qty, src, dst, expected):
        assert convert(Decimal(qty), src, dst) == Decimal(expected)

    def test_identity_short_circuits(self):
        assert convert(Decimal("7.5"), "g", "g") == Decimal("7.5")

    def test_case_and_whitespace_insensitive(self):
        assert convert(Decimal("1"), " KG ", "G") == Decimal("1000")

    def test_across_dimensions_raises(self):
        # Mass and count are not interchangeable without an ingredient-specific
        # rule ("one egg weighs 50 g"), so this must fail loudly.
        with pytest.raises(UomError, match="Cannot convert"):
            convert(Decimal("1"), "g", "ea")

    def test_error_names_the_remedy(self):
        with pytest.raises(UomError, match="uom_conversion"):
            convert(Decimal("1"), "ml", "ea")


class TestHelpers:
    def test_dimension_of(self):
        assert dimension_of("kg") == "g"
        assert dimension_of("l") == "ml"
        assert dimension_of("portion") == "ea"
        assert dimension_of("furlong") is None

    def test_compatible(self):
        assert compatible("g", "kg")
        assert compatible("ml", "l")
        assert not compatible("g", "ml")
        assert not compatible("ea", "kg")
