"""Explicit classification of Cost Explorer RECORD_TYPE values.

NOT YET VERIFIED against the API. The names below come from the Cost Explorer user guide's console
"Charge type" list (source CONSOLE_DOC) or are candidates from AWS billing line-item types
(source CANDIDATE). The API's exact strings may differ, so matching ignores case, spaces, hyphens and
underscores. Confirm with `aws ce get-dimension-values --dimension RECORD_TYPE` (docs/cost-intelligence.md).

Anything that does not match is UNCLASSIFIED: it is reported by name and never counted as a charge.
"""

from __future__ import annotations

import re
from enum import StrEnum


class Category(StrEnum):
    USAGE = "usage"
    SAVINGS_PLAN_NEGATION = "savings_plan_negation"
    FEE = "fee"
    TAX = "tax"
    DISCOUNT = "discount"
    CREDIT = "credit"
    REFUND = "refund"
    UNCLASSIFIED = "unclassified"


class MatchSource(StrEnum):
    CONSOLE_DOC = "console_doc"  # named in the Cost Explorer user guide's Charge type list
    CANDIDATE = "candidate"  # plausible API spelling or billing line-item type; unconfirmed


# Categories that make up a service's cost (before discounts, credits and refunds).
CHARGE_CATEGORIES = frozenset({Category.USAGE, Category.SAVINGS_PLAN_NEGATION, Category.FEE, Category.TAX})

_C, _K = MatchSource.CONSOLE_DOC, MatchSource.CANDIDATE
_TABLE: dict[str, tuple[Category, MatchSource]] = {
    "Usage": (Category.USAGE, _C),
    "Reservation applied usage": (Category.USAGE, _C),
    "Savings Plan covered usage": (Category.USAGE, _C),
    "DiscountedUsage": (Category.USAGE, _K),
    "Savings Plan negation": (Category.SAVINGS_PLAN_NEGATION, _C),
    "Recurring reservation fee": (Category.FEE, _C),
    "Upfront reservation fee": (Category.FEE, _C),
    "Savings Plan recurring fee": (Category.FEE, _C),
    "Savings Plan upfront fee": (Category.FEE, _C),
    "Support fee": (Category.FEE, _C),
    "Other out-of-cycle charges": (Category.FEE, _C),
    "Tax": (Category.TAX, _C),
    "Bundled discount": (Category.DISCOUNT, _K),
    "Enterprise discount program discount": (Category.DISCOUNT, _K),
    "EdpDiscount": (Category.DISCOUNT, _K),
    "Private rate discount": (Category.DISCOUNT, _K),
    "Distributor discount": (Category.DISCOUNT, _K),
    "Solution provider program discount": (Category.DISCOUNT, _K),
    "SppDiscount": (Category.DISCOUNT, _K),
    "Credit": (Category.CREDIT, _C),
    "Refund": (Category.REFUND, _C),
}


def _norm(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value).lower()


_INDEX = {_norm(name): entry for name, entry in _TABLE.items()}


def classify(record_type: str) -> tuple[Category, MatchSource | None]:
    return _INDEX.get(_norm(record_type), (Category.UNCLASSIFIED, None))
