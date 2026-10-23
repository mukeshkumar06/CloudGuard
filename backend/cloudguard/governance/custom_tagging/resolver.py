"""Resolve TargetCriteria against discovered resources (whose tags were already inspected).

Specific targets must match exactly one resource. Names match case-sensitively; a case-insensitive
near-miss is offered for clarification, never auto-selected. Bulk targets return every resource of
the stated type (and region, if given) - never more, and never inferred from a specific request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from cloudguard.governance.custom_tagging.models import Candidate, TargetCriteria
from cloudguard.governance.models import UNRESOLVED_REGION, DiscoveredResource, DiscoveryResult


class ResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"  # several exact matches
    NEAR_MISS = "NEAR_MISS"  # only case-insensitive matches; ask instead of guessing
    OUT_OF_SCOPE = "OUT_OF_SCOPE"  # another account, or a region that wasn't discovered


@dataclass(frozen=True)
class Resolution:
    status: ResolutionStatus
    resources: list[DiscoveredResource] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    message: str = ""
    # Bulk only: resources of the requested type that the request's explicit criteria leave out,
    # reported so the user sees what was deliberately not included.
    out_of_scope: list[tuple[DiscoveredResource, str]] = field(default_factory=list)


def _candidate(r: DiscoveredResource) -> Candidate:
    return Candidate(resource_type=r.resource_type, resource_name=r.resource_name, resource_arn=r.resource_arn, region=r.region)


def _unique(resources: list[DiscoveredResource]) -> list[DiscoveredResource]:
    seen: set[str] = set()
    out = []
    for r in resources:
        if r.resource_arn not in seen:
            seen.add(r.resource_arn)
            out.append(r)
    return out


def resolve(target: TargetCriteria, discovery: DiscoveryResult) -> Resolution:
    if target.account_id and target.account_id != discovery.account_id:
        return Resolution(ResolutionStatus.OUT_OF_SCOPE,
                          message=f"account {target.account_id} is not the inspected account; CloudGuard only tags the authenticated account")
    if target.region and target.region not in discovery.regions:
        return Resolution(ResolutionStatus.OUT_OF_SCOPE,
                          message=f"region {target.region} was not discovered (scanned: {', '.join(discovery.regions)})")

    pool = _unique(discovery.resources)
    if target.resource_type:
        pool = [r for r in pool if r.resource_type == target.resource_type]
    excluded_by_region: list[tuple[DiscoveredResource, str]] = []
    if target.region:
        # A bucket whose region couldn't be resolved might be in the requested region; keep it
        # (it will surface as INSPECTION_FAILED) rather than silently calling it out of scope.
        excluded_by_region = [(r, f"in {r.region}; the request is limited to {target.region}")
                              for r in pool if r.region not in (target.region, UNRESOLVED_REGION)]
        pool = [r for r in pool if r.region in (target.region, UNRESOLVED_REGION)]

    if target.all_of_type:
        if target.arns is not None:
            # An explicit selection: every ARN must be a discovered resource in scope, or nothing is proposed.
            by_arn = {r.resource_arn: r for r in pool}
            missing = [a for a in target.arns if a not in by_arn]
            if missing:
                return Resolution(ResolutionStatus.NOT_FOUND,
                                  message=f"{len(missing)} selected resource(s) were not found in scope: "
                                          f"{', '.join(missing[:5])}")
            pool = [by_arn[a] for a in target.arns]
        if target.name_filter is not None:
            pool = [r for r in pool if target.name_filter.matches(r.resource_name)]
        what = target.resource_type or "matching"
        if not pool:
            where = f" in {target.region}" if target.region else ""
            named = f" whose names {target.name_filter.match.replace('_', ' ')} the given text" if target.name_filter else ""
            return Resolution(ResolutionStatus.NOT_FOUND, message=f"no {what} resources{named} found{where}")
        return Resolution(ResolutionStatus.RESOLVED, resources=pool, out_of_scope=excluded_by_region)

    if target.arn:
        hits = [r for r in pool if r.resource_arn == target.arn]
        if not hits:
            return Resolution(ResolutionStatus.NOT_FOUND, message=f"no discovered resource has ARN {target.arn}")
        return Resolution(ResolutionStatus.RESOLVED, resources=hits)

    exact = [r for r in pool if r.resource_name == target.name or r.resource_id == target.name]
    if len(exact) == 1:
        return Resolution(ResolutionStatus.RESOLVED, resources=exact)
    if len(exact) > 1:
        return Resolution(ResolutionStatus.AMBIGUOUS, candidates=[_candidate(r) for r in exact],
                          message=f"{len(exact)} resources are named {target.name!r}; specify the type, region or ARN")
    near = [r for r in pool if r.resource_name.lower() == target.name.lower() or r.resource_id.lower() == target.name.lower()]
    if near:
        return Resolution(ResolutionStatus.NEAR_MISS, candidates=[_candidate(r) for r in near],
                          message=f"no resource is named exactly {target.name!r}; names are case-sensitive - did you mean one of these?")
    return Resolution(ResolutionStatus.NOT_FOUND, message=f"no resource named {target.name!r} was found")
