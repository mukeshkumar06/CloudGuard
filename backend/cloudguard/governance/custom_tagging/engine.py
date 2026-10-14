"""Custom tagging workflow: request -> validation -> fresh inspection -> resolution -> proposal.

Read-only. The engine never calls an AWS write API; it produces an immutable, hashed, expiring
proposal that Phase 4 will present for authenticated approval and execute after re-validation.

Every considered resource gets exactly one outcome (see ResourceOutcome):
- CHANGE_PROPOSED   at least one tag would be added, or overwritten by an update operation
- ALREADY_MATCHING  every requested tag already has the requested value
- CONFLICT          nothing to change because of WOULD_OVERWRITE (add ops never overwrite),
                    KEY_NOT_PRESENT (updates never add), KEY_CASE_CONFLICT (no near-duplicate
                    keys) or TAG_LIMIT_EXCEEDED (AWS allows at most 50 user tags)
- POLICY_VIOLATION  a requested value breaks the tagging policy for this resource type; the whole
                    proposal is then NOT_APPROVABLE
- INSPECTION_FAILED current tags unknown, so no change can be proven safe
- OUT_OF_SCOPE      bulk only: same type, but excluded by the request's explicit region
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from cloudguard.governance.compliance import evaluate
from cloudguard.governance.custom_tagging.models import (
    ApprovalStatus,
    ChangeAction,
    ConflictReason,
    MatchingTag,
    OutcomeStatus,
    PolicyViolation,
    Rejection,
    RejectionCode,
    ResourceOutcome,
    ResourceRecord,
    TagChange,
    TagConflict,
    TaggingOutcome,
    TaggingProposal,
    TaggingRequest,
    proposal_hash,
    summarize_records,
)
from cloudguard.governance.custom_tagging.parser import CompositeParser, RequestParser, RuleBasedParser
from cloudguard.governance.custom_tagging.resolver import ResolutionStatus, resolve
from cloudguard.governance.custom_tagging.validation import (
    MAX_TAGS_PER_RESOURCE,
    classify_and_check,
    validate_tag,
)
from cloudguard.governance.discovery import DiscoverySource
from cloudguard.governance.models import DiscoveredResource
from cloudguard.governance.policy import PolicyVersion

DEFAULT_PROPOSAL_TTL = timedelta(minutes=15)


def _user_tag_count(tags: dict[str, str]) -> int:
    return sum(1 for k in tags if not k.lower().startswith("aws:"))  # aws: tags don't count toward the limit


class CustomTaggingEngine:
    def __init__(
        self,
        source: DiscoverySource,
        policy: PolicyVersion,
        parser: RequestParser | None = None,
        proposal_ttl: timedelta = DEFAULT_PROPOSAL_TTL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.source = source
        self.policy = policy
        self.parser = parser or CompositeParser(RuleBasedParser())
        self.proposal_ttl = proposal_ttl
        self.clock = clock

    # ------------------------------------------------------------------ entry points

    def prepare(self, text: str) -> TaggingOutcome:
        parsed = self.parser.parse(text)
        if parsed is None:  # a bare RuleBasedParser can return None
            parsed = CompositeParser().parse(text)
        if isinstance(parsed, Rejection):
            return TaggingOutcome(status=OutcomeStatus.REJECTED, message=parsed.message, rejection=parsed)
        return self.prepare_request(parsed)

    def prepare_request(self, request: TaggingRequest) -> TaggingOutcome:
        """Validate a structured request from any parser and build a proposal. Read-only."""
        for key, value in request.tags.items():
            if (bad := validate_tag(key, value)) is not None:
                return self._rejected(bad, request)

        discovery = self.source.discover()  # fresh, read-only inspection of current tags
        resolution = resolve(request.target, discovery)
        if resolution.status == ResolutionStatus.OUT_OF_SCOPE:
            return self._rejected(Rejection(code=RejectionCode.OUT_OF_SCOPE, message=resolution.message), request)
        if resolution.status == ResolutionStatus.NOT_FOUND:
            return TaggingOutcome(status=OutcomeStatus.NOT_FOUND, message=resolution.message, request=request)
        if resolution.status in (ResolutionStatus.AMBIGUOUS, ResolutionStatus.NEAR_MISS):
            return TaggingOutcome(status=OutcomeStatus.NEEDS_CLARIFICATION, message=resolution.message,
                                  request=request, candidates=resolution.candidates)

        records = [self._record(request, r) for r in resolution.resources]
        records += [self._out_of_scope(r, reason) for r, reason in resolution.out_of_scope]
        proposal = self._build_proposal(request, discovery.account_id, records)
        if proposal.is_approvable:
            status = OutcomeStatus.PROPOSAL_READY
        elif proposal.impact.resources_policy_violations:
            status = OutcomeStatus.BLOCKED
        else:
            status = OutcomeStatus.NO_CHANGES
        return TaggingOutcome(status=status, message=self._describe(proposal), request=request, proposal=proposal)

    # ------------------------------------------------------------------ per-resource records

    @staticmethod
    def _out_of_scope(r: DiscoveredResource, reason: str) -> ResourceRecord:
        return ResourceRecord(
            outcome=ResourceOutcome.OUT_OF_SCOPE, resource_type=r.resource_type, resource_name=r.resource_name,
            resource_arn=r.resource_arn, account_id=r.account_id, region=r.region, current_tags=None,
            tags_inspected_at=None, out_of_scope_reason=reason,
        )

    def _record(self, request: TaggingRequest, r: DiscoveredResource) -> ResourceRecord:
        common = dict(resource_type=r.resource_type, resource_name=r.resource_name, resource_arn=r.resource_arn,
                      account_id=r.account_id, region=r.region)
        if r.tags is None:
            return ResourceRecord(outcome=ResourceOutcome.INSPECTION_FAILED, **common, current_tags=None,
                                  tags_inspected_at=r.discovered_at, inspection_error=r.tag_error)

        rules = self.policy.document.rules_for(r.resource_type)
        changes: list[TagChange] = []
        matching: list[MatchingTag] = []
        conflicts: list[TagConflict] = []
        violations: list[PolicyViolation] = []
        for key, value in request.tags.items():
            category, violation, notes = classify_and_check(key, value, rules)
            if violation is not None:
                violations.append(violation)
                continue
            case_variants = [k for k in r.tags if k.lower() == key.lower() and k != key]
            if key in r.tags:
                current = r.tags[key]
                if current == value:
                    matching.append(MatchingTag(key=key, value=value))
                elif request.operation.is_update:
                    changes.append(TagChange(key=key, action=ChangeAction.OVERWRITE, old_value=current,
                                             new_value=value, category=category, notes=notes))
                else:
                    conflicts.append(TagConflict(key=key, reason=ConflictReason.WOULD_OVERWRITE, requested_value=value,
                                                 existing_key=key, existing_value=current,
                                                 detail="add operations never overwrite; use an update request"))
            elif case_variants:
                conflicts.append(TagConflict(key=key, reason=ConflictReason.KEY_CASE_CONFLICT, requested_value=value,
                                             existing_key=case_variants[0], existing_value=r.tags[case_variants[0]],
                                             detail="the resource already has this key in a different case"))
            elif request.operation.is_update:
                conflicts.append(TagConflict(key=key, reason=ConflictReason.KEY_NOT_PRESENT, requested_value=value,
                                             detail="update operations only change existing keys"))
            else:
                changes.append(TagChange(key=key, action=ChangeAction.ADD, old_value=None, new_value=value,
                                         category=category, notes=notes))

        added = [c for c in changes if c.action == ChangeAction.ADD]
        if added and _user_tag_count(r.tags) + len(added) > MAX_TAGS_PER_RESOURCE:
            detail = (f"{_user_tag_count(r.tags)} existing + {len(added)} new tags exceeds AWS's limit of "
                      f"{MAX_TAGS_PER_RESOURCE}")
            conflicts += [TagConflict(key=c.key, reason=ConflictReason.TAG_LIMIT_EXCEEDED, requested_value=c.new_value,
                                      detail=detail) for c in added]
            changes = [c for c in changes if c.action != ChangeAction.ADD]

        if violations:
            # Nothing on a violating resource is proposed (the proposal is NOT_APPROVABLE anyway).
            # Violating keys never reach matching/conflicts, so each key still appears at most once.
            outcome = ResourceOutcome.POLICY_VIOLATION
            changes = []
        elif changes:
            outcome = ResourceOutcome.CHANGE_PROPOSED
        elif conflicts:
            outcome = ResourceOutcome.CONFLICT
        else:
            outcome = ResourceOutcome.ALREADY_MATCHING

        projected_tags = {**r.tags, **{c.key: c.new_value for c in changes}}
        projected = evaluate(r.model_copy(update={"tags": projected_tags}), rules, self.clock())
        return ResourceRecord(
            outcome=outcome, **common, current_tags=dict(r.tags), tags_inspected_at=r.discovered_at,
            changes=changes, matching=matching, conflicts=conflicts, policy_violations=violations,
            projected_status=projected.compliance_status, projected_missing_tags=projected.missing_tags,
        )

    # ------------------------------------------------------------------ proposal

    def _build_proposal(self, request: TaggingRequest, account_id: str, records: list[ResourceRecord]) -> TaggingProposal:
        impact = summarize_records(records)
        blocking: list[str] = []
        if impact.resources_policy_violations:
            blocking.append(f"{impact.resources_policy_violations} resource(s) would violate the tagging policy")
        if not impact.resources_changing:
            blocking.append("no tag would change")
        now = self.clock()
        draft = TaggingProposal.model_construct(
            proposal_id=f"proposal-{uuid.uuid4()}",
            operation=request.operation,
            request=request,
            account_id=account_id,
            policy=self.policy.ref,
            created_at=now,
            expires_at=now + self.proposal_ttl,
            approval_status=ApprovalStatus.NOT_APPROVABLE if blocking else ApprovalStatus.PENDING,
            blocking_reasons=blocking,
            records=records,
            impact=impact,
            requires_overwrite_confirmation=impact.tags_overwritten > 0,
            content_hash="",
        )
        data = draft.model_dump(mode="json")
        data["schema_version"] = TaggingProposal.model_fields["schema_version"].default
        data["content_hash"] = proposal_hash(data)
        return TaggingProposal.model_validate(data)  # re-validates every integrity rule

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _rejected(rejection: Rejection, request: TaggingRequest | None = None) -> TaggingOutcome:
        return TaggingOutcome(status=OutcomeStatus.REJECTED, message=rejection.message, request=request, rejection=rejection)

    @staticmethod
    def _describe(p: TaggingProposal) -> str:
        i = p.impact
        parts = [f"{i.resources_matched} resource(s) matched"]
        for count, label in (
            (i.resources_changing, "would change"),
            (i.resources_already_matching, "already match"),
            (i.resources_conflicting, "conflict"),
            (i.resources_policy_violations, "violate the tagging policy"),
            (i.resources_inspection_failed, "could not be inspected"),
            (i.resources_out_of_scope, "out of scope (not included)"),
        ):
            if count:
                parts.append(f"{count} {label}")
        if i.tags_added:
            parts.append(f"{i.tags_added} tag(s) to add")
        if i.tags_overwritten:
            parts.append(f"{i.tags_overwritten} existing value(s) to OVERWRITE - requires explicit confirmation")
        lead = "Proposal ready for approval" if p.is_approvable else f"Not approvable ({'; '.join(p.blocking_reasons)})"
        return f"{lead}: " + "; ".join(parts) + ". Nothing has been changed in AWS."
