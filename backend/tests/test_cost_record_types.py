"""RECORD_TYPE classification: explicit table, tolerant spelling, unknowns stay unclassified."""

import pytest

from cloudguard.cost.record_types import CHARGE_CATEGORIES, Category, MatchSource, classify


@pytest.mark.parametrize("value,category", [
    ("Usage", Category.USAGE),
    ("Reservation applied usage", Category.USAGE),
    ("SavingsPlanCoveredUsage", Category.USAGE),
    ("DiscountedUsage", Category.USAGE),
    ("Savings Plan Negation", Category.SAVINGS_PLAN_NEGATION),
    ("savings_plan_negation", Category.SAVINGS_PLAN_NEGATION),
    ("Recurring reservation fee", Category.FEE),
    ("UpfrontReservationFee", Category.FEE),
    ("Savings Plan recurring fee", Category.FEE),
    ("Savings Plan upfront fee", Category.FEE),
    ("Support fee", Category.FEE),
    ("Other out-of-cycle charges", Category.FEE),
    ("TAX", Category.TAX),
    ("Bundled Discount", Category.DISCOUNT),
    ("EdpDiscount", Category.DISCOUNT),
    ("Private rate discount", Category.DISCOUNT),
    ("credit", Category.CREDIT),
    ("Refund", Category.REFUND),
])
def test_known_record_types(value, category):
    assert classify(value)[0] == category


@pytest.mark.parametrize("value", ["", "Fee", "Discount", "Credits", "Marketplace usage", "RIFee", "Usage (estimated)"])
def test_unknown_record_types_are_unclassified_never_charges(value):
    category, source = classify(value)
    assert category == Category.UNCLASSIFIED and source is None
    assert category not in CHARGE_CATEGORIES


def test_match_source_distinguishes_documented_from_candidate():
    assert classify("Credit")[1] == MatchSource.CONSOLE_DOC
    assert classify("Bundled discount")[1] == MatchSource.CANDIDATE


def test_only_charge_categories_feed_service_totals():
    assert CHARGE_CATEGORIES == {Category.USAGE, Category.SAVINGS_PLAN_NEGATION, Category.FEE, Category.TAX}
