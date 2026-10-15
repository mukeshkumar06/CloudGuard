"""Thin Cost Explorer access: client factory, capped and counted pagination, error reduction.

Every Cost Explorer request is billed (about $0.01 per paginated request), so pages are capped and
each request sent is counted. botocore retries (max_attempts=3) happen below this layer and are not
counted; they may also be billed.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config

from cloudguard.cost.models import CostError
from cloudguard.governance.discovery.common import classify_error
from cloudguard.governance.models import ErrorKind

# Cost Explorer has a single endpoint regardless of where the account's resources live.
CE_REGION = "us-east-1"
CE_CONFIG = Config(retries={"mode": "standard", "max_attempts": 3}, user_agent_extra="cloudguard-ai")

DEFAULT_MAX_PAGES = 5
MAX_PAGES_LIMIT = 20
MAX_PAGES_ENV = "CLOUDGUARD_CE_MAX_PAGES"

DATA_UNAVAILABLE = "DataUnavailableException"

# AWS's documented meanings (GetCostAndUsage / GetAnomalies API reference, checked 2026-10-15).
AWS_ERROR_DESCRIPTIONS = {
    "DataUnavailableException": "The requested data is unavailable.",
    "BillExpirationException": "The requested report expired. Update the date interval and try again.",
    "LimitExceededException": "You made too many calls in a short period of time. Try again later.",
    "InvalidNextTokenException": "The pagination token is invalid. Try again without a pagination token.",
    "RequestChangedException": "Your request parameters changed between pages.",
    "ResourceNotFoundException": "The specified ARN in the request doesn't exist.",
    "BillingViewHealthStatusException": "The billing view status must be HEALTHY to perform this action.",
}

# CloudGuard's guesses. Reported separately from anything AWS said.
INFERRED_CAUSES = {
    DATA_UNAVAILABLE: [
        "Cost Explorer may not be enabled for this account yet",
        "AWS may still be preparing cost data (for example, shortly after Cost Explorer was enabled)",
        "the requested period may be outside the data Cost Explorer holds",
    ],
    "ACCESS_DENIED": [
        "the identity may lack the required ce: permission",
        "IAM access to billing information may not be activated for this account",
    ],
}


class PageLimitExceeded(Exception):
    """More pages than allowed: the result would be incomplete, so it is not used."""


@dataclass
class RequestLog:
    """Counts Cost Explorer requests sent and records when AWS last answered."""

    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    count: int = 0
    last_success_at: datetime | None = None


def make_client(session: boto3.Session):
    return session.client("ce", region_name=CE_REGION, config=CE_CONFIG)


def resolve_max_pages(value: int | None = None) -> int:
    """Explicit value, else $CLOUDGUARD_CE_MAX_PAGES, else the default; always 1..MAX_PAGES_LIMIT."""
    if value is None:
        raw = os.environ.get(MAX_PAGES_ENV)
        try:
            value = int(raw) if raw else DEFAULT_MAX_PAGES
        except ValueError:
            raise ValueError(f"{MAX_PAGES_ENV} must be an integer") from None
    if not 1 <= value <= MAX_PAGES_LIMIT:
        raise ValueError(f"max_pages must be between 1 and {MAX_PAGES_LIMIT}")
    return value


def paginate(call: Callable[..., dict], items_key: str, log: RequestLog, max_pages: int, **kwargs: Any) -> list:
    """Collect `items_key` across NextPageToken pages, refusing to return a truncated result."""
    items: list = []
    token = None
    for _ in range(max_pages):
        log.count += 1
        resp = call(**kwargs, **({"NextPageToken": token} if token else {}))
        log.last_success_at = log.clock()
        items.extend(resp.get(items_key, []))
        token = resp.get("NextPageToken")
        if not token:
            return items
    raise PageLimitExceeded(f"more than {max_pages} pages")


def reduce_error(exc: Exception, operation: str) -> tuple[CostError, bool]:
    """(error with AWS's code and documented meaning but not its message, is_access_denied)."""
    if isinstance(exc, PageLimitExceeded):
        return CostError(code="PageLimitExceeded", operation=operation), False
    classified = classify_error(exc, operation)
    denied = classified.kind == ErrorKind.ACCESS_DENIED
    causes = INFERRED_CAUSES.get("ACCESS_DENIED" if denied else classified.code, [])
    return CostError(code=classified.code, operation=operation,
                     aws_description=AWS_ERROR_DESCRIPTIONS.get(classified.code),
                     inferred_possible_causes=causes), denied
