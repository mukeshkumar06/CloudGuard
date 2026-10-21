"""Offline Cost Explorer stand-in for fixture mode: no boto3, no credentials, no network.

It answers the three read-only operations the cost code uses (GetCostAndUsage, GetAnomalyMonitors,
GetAnomalies) from a synthetic JSON file, in AWS's response shapes. It also pages (NextPageToken) and
raises botocore ClientErrors, so fixture runs go through the same pagination and error handling as live
runs.

Fixture format (all amounts are decimal strings):

    {
      "description": "...", "synthetic": true, "as_of": "YYYY-MM-DD", "currency": "USD",
      "cost_and_usage": {
        "page_size": 2,                       # optional: groups per page
        "fail": {"code": "...", "http_status": 400, "on_page": 1},   # optional
        "periods": {"2026-09-01/2026-10-01": {"estimated": false,
                                               "lines": [["SERVICE", "RECORD_TYPE", "1.23"], ...]}}
      },
      "anomaly_monitors": {"items": [...], "page_size": 1, "fail": {...}},   # raw AWS objects
      "anomalies": {"items": [...], "page_size": 1, "fail": {...}}           # raw AWS objects
    }

A GetCostAndUsage request is split into calendar-month segments; each segment is looked up by its exact
"start/end_exclusive" key. A segment with no entry returns no groups (Cost Explorer's "no data" shape).
GetAnomalies returns the anomalies whose dates overlap the requested interval.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from botocore.exceptions import ClientError

from cloudguard.cost.models import METRIC
from cloudguard.cost.periods import next_month

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "cost"
DEFAULT_FIXTURE = FIXTURE_DIR / "demo.json"
_GROUP_BY = [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "RECORD_TYPE"}]


class FixtureError(Exception):
    """The fixture file is missing, malformed, or the request is one the real code never sends."""


def resolve_fixture(value: str | Path | None) -> Path:
    """A path, or a scenario name under fixtures/cost (for example "decrease")."""
    if value is None:
        return DEFAULT_FIXTURE
    path = Path(value)
    if not path.exists() and path.suffix == "" and (FIXTURE_DIR / f"{value}.json").exists():
        path = FIXTURE_DIR / f"{value}.json"
    if not path.is_file():
        raise FixtureError(f"fixture not found: {value}")
    return path


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


class FixtureCostExplorer:
    """Duck-types the boto3 Cost Explorer client methods used by bill, compare and anomalies."""

    def __init__(self, data: dict, name: str = "fixture") -> None:
        if not isinstance(data, dict) or data.get("synthetic") is not True:
            raise FixtureError("cost fixtures must be synthetic and say so with \"synthetic\": true")
        self.name = name
        self.description = data.get("description", "")
        self.as_of = date.fromisoformat(data["as_of"])
        self.currency = data.get("currency", "USD")
        self._ce = data.get("cost_and_usage", {})
        self._monitors = data.get("anomaly_monitors", {})
        self._anomalies = data.get("anomalies", {})
        self.calls: list[str] = []

    @classmethod
    def load(cls, value: str | Path | None = None) -> FixtureCostExplorer:
        path = resolve_fixture(value)
        try:
            return cls(json.loads(path.read_text(encoding="utf-8")), name=path.name)
        except (ValueError, KeyError, TypeError) as exc:
            raise FixtureError(f"invalid cost fixture {path.name}: {type(exc).__name__}: {exc}") from None

    # ---- shared paging and failure ----

    def _page(self, operation: str, section: dict, chunks: list, kwargs: dict) -> int:
        """Index of the requested page, after checking the token and raising any configured failure."""
        self.calls.append(operation)
        token = kwargs.get("NextPageToken")
        index = 0
        if token is not None:
            if not token.startswith("fixture-page-") or not token[13:].isdigit() or int(token[13:]) >= len(chunks):
                raise ClientError({"Error": {"Code": "InvalidNextTokenException", "Message": "invalid token"},
                                   "ResponseMetadata": {"HTTPStatusCode": 400}}, operation)
            index = int(token[13:])
        fail = section.get("fail")
        if fail and index + 1 == fail.get("on_page", 1):
            raise ClientError({"Error": {"Code": fail["code"], "Message": "synthetic fixture failure"},
                               "ResponseMetadata": {"HTTPStatusCode": fail.get("http_status", 400)}}, operation)
        return index

    @staticmethod
    def _chunks(items: list, page_size: int | None) -> list[list]:
        if not page_size or not items:
            return [items]
        return [items[i:i + page_size] for i in range(0, len(items), page_size)]

    @staticmethod
    def _token(index: int, chunks: list) -> dict:
        return {"NextPageToken": f"fixture-page-{index + 1}"} if index + 1 < len(chunks) else {}

    # ---- GetCostAndUsage ----

    def get_cost_and_usage(self, **kwargs) -> dict:
        if (kwargs.get("Granularity") != "MONTHLY" or kwargs.get("Metrics") != [METRIC]
                or kwargs.get("GroupBy") != _GROUP_BY):
            raise FixtureError("fixture only serves MONTHLY UnblendedCost grouped by SERVICE and RECORD_TYPE")
        start = date.fromisoformat(kwargs["TimePeriod"]["Start"])
        end = date.fromisoformat(kwargs["TimePeriod"]["End"])
        periods = self._ce.get("periods", {})
        results: list[dict] = []
        flat: list[tuple[int, dict]] = []
        cursor = start
        while cursor < end:
            seg_end = min(next_month(cursor), end)
            entry = periods.get(f"{cursor.isoformat()}/{seg_end.isoformat()}", {})
            results.append({"TimePeriod": {"Start": cursor.isoformat(), "End": seg_end.isoformat()},
                            "Total": {}, "Groups": [], "Estimated": bool(entry.get("estimated", False))})
            for line in entry.get("lines", []):
                service, record_type, amount, *unit = line
                flat.append((len(results) - 1, {"Keys": [service, record_type], "Metrics": {
                    METRIC: {"Amount": str(amount), "Unit": unit[0] if unit else self.currency}}}))
            cursor = seg_end

        chunks = self._chunks(flat, self._ce.get("page_size"))
        index = self._page("GetCostAndUsage", self._ce, chunks, kwargs)
        page: dict[int, dict] = {}
        if index == 0:  # periods without groups appear once, on the first page
            page = {i: {**r, "Groups": []} for i, r in enumerate(results)}
        for i, group in chunks[index]:
            page.setdefault(i, {**results[i], "Groups": []})["Groups"].append(group)
        return {"ResultsByTime": [page[i] for i in sorted(page)], "DimensionValueAttributes": [],
                **self._token(index, chunks)}

    # ---- Cost Anomaly Detection ----

    def get_anomaly_monitors(self, **kwargs) -> dict:
        chunks = self._chunks(list(self._monitors.get("items", [])), self._monitors.get("page_size"))
        index = self._page("GetAnomalyMonitors", self._monitors, chunks, kwargs)
        return {"AnomalyMonitors": chunks[index], **self._token(index, chunks)}

    def get_anomalies(self, **kwargs) -> dict:
        window = kwargs["DateInterval"]
        lo, hi = _date(window["StartDate"]), _date(window.get("EndDate"))
        items = [a for a in self._anomalies.get("items", [])
                 if (hi is None or _date(a.get("AnomalyStartDate")) <= hi)
                 and (a.get("AnomalyEndDate") is None or _date(a["AnomalyEndDate"]) >= lo)]
        chunks = self._chunks(items, self._anomalies.get("page_size"))
        index = self._page("GetAnomalies", self._anomalies, chunks, kwargs)
        return {"Anomalies": chunks[index], **self._token(index, chunks)}
