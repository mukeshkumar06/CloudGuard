"""Phase 3: custom tagging requests -> read-only proposals. Synthetic fixtures and stubbed AWS only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber
from pydantic import ValidationError

from cloudguard.governance.__main__ import main
from cloudguard.governance.custom_tagging import CompositeParser, CustomTaggingEngine, RuleBasedParser
from cloudguard.governance.custom_tagging.models import (
    ApprovalStatus,
    ChangeAction,
    ConflictReason,
    OutcomeStatus,
    RejectionCode,
    ResourceOutcome,
    ResourceRecord,
    TagCategory,
    TaggingOperation,
    TaggingProposal,
    TaggingRequest,
    TargetCriteria,
    proposal_hash,
    summarize_records,
)
from cloudguard.governance.discovery import FixtureSource, LiveSource
from cloudguard.governance.models import ComplianceStatus
from cloudguard.governance.policy import DEFAULT_POLICY_DOCUMENT, parse_policy
from cloudguard.governance.policy_store import LocalPolicyStore, PolicyManager

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "custom-tagging-demo.json"
NOW = datetime(2026, 10, 14, 6, 30, tzinfo=UTC)
ACCOUNT = "123456789012"


def policy(doc=DEFAULT_POLICY_DOCUMENT):
    m = PolicyManager(LocalPolicyStore())
    v1 = m.ensure_default()
    return v1 if doc == DEFAULT_POLICY_DOCUMENT else m.save(doc, expected_version=1, created_by="test")


class Static:
    mode = "fixture"

    def __init__(self, discovery):
        self.discovery = discovery
        self.calls = 0

    def discover(self):
        self.calls += 1
        return self.discovery


def engine(source=None, pol=None, **kw):
    return CustomTaggingEngine(source or FixtureSource(FIXTURE), pol or policy(), clock=lambda: NOW, **kw)


def prepare(text, **kw):
    return engine(**kw).prepare(text)


def rec(outcome, name, region=None):
    return next(r for r in outcome.proposal.records
                if r.resource_name == name and (region is None or r.region == region))


# =========================================================================== parser

PARSE_OK = [
    ("Tag the Lambda function ServiceWRK with ServiceWRK=Lambda.", TaggingOperation.ADD_TAG,
     dict(resource_type="AWS::Lambda::Function", name="ServiceWRK"), {"ServiceWRK": "Lambda"}),
    ("tag lambda ServiceWRK with Team=core, Tier=1", TaggingOperation.ADD_TAG,
     dict(resource_type="AWS::Lambda::Function", name="ServiceWRK"), {"Team": "core", "Tier": "1"}),
    ('Add Owner="Platform and Data" and Tier=gold to dynamodb table orders', TaggingOperation.ADD_TAG,
     dict(resource_type="AWS::DynamoDB::Table", name="orders"), {"Owner": "Platform and Data", "Tier": "gold"}),
    ("Tag all Lambda functions with Project=CloudGuard.", TaggingOperation.BULK_ADD_TAG,
     dict(resource_type="AWS::Lambda::Function", all_of_type=True), {"Project": "CloudGuard"}),
    ("Add Environment=Development to all DynamoDB tables", TaggingOperation.BULK_ADD_TAG,
     dict(resource_type="AWS::DynamoDB::Table", all_of_type=True), {"Environment": "Development"}),
    ("tag all lambda functions in us-east-1 with Team=edge", TaggingOperation.BULK_ADD_TAG,
     dict(resource_type="AWS::Lambda::Function", all_of_type=True, region="us-east-1"), {"Team": "edge"}),
    ("Update Environment of ServiceWRK to Production", TaggingOperation.UPDATE_TAG,
     dict(name="ServiceWRK"), {"Environment": "Production"}),
    ("Update the Owner tag of Lambda function ServiceWRK to BackendTeam", TaggingOperation.UPDATE_TAG,
     dict(resource_type="AWS::Lambda::Function", name="ServiceWRK"), {"Owner": "BackendTeam"}),
    ('change "Cost Center" on s3 bucket acme-logs to "CC 42"', TaggingOperation.UPDATE_TAG,
     dict(resource_type="AWS::S3::Bucket", name="acme-logs"), {"Cost Center": "CC 42"}),
    ("Update Owner of all EC2 instances in ap-south-1 in account 123456789012 to platform", TaggingOperation.BULK_UPDATE_TAG,
     dict(resource_type="AWS::EC2::Instance", all_of_type=True, region="ap-south-1", account_id=ACCOUNT), {"Owner": "platform"}),
    ("Tag arn:aws:lambda:ap-south-1:123456789012:function:worker with A=B", TaggingOperation.ADD_TAG,
     dict(arn="arn:aws:lambda:ap-south-1:123456789012:function:worker"), {"A": "B"}),
    ("tag lambda function worker in region us-east-1 with A=B", TaggingOperation.ADD_TAG,
     dict(resource_type="AWS::Lambda::Function", name="worker", region="us-east-1"), {"A": "B"}),
]


@pytest.mark.parametrize(("text", "op", "target", "tags"), PARSE_OK)
def test_rule_parser_supported_requests(text, op, target, tags):
    req = RuleBasedParser().parse(text)
    assert isinstance(req, TaggingRequest), req
    assert req.operation == op and req.tags == tags and req.parsed_by == "rules"
    assert req.target == TargetCriteria(**target)
    assert req.raw_text == text


@pytest.mark.parametrize(("text", "code"), [
    ("Remove Owner from ServiceWRK", RejectionCode.UNSUPPORTED_REQUEST),
    ("delete all tags on bucket acme-logs", RejectionCode.UNSUPPORTED_REQUEST),
    ("Tag Lambda functions with Project=X", RejectionCode.AMBIGUOUS_REQUEST),      # plural without "all"
    ("tag dynamodb tables with A=B", RejectionCode.AMBIGUOUS_REQUEST),
    ("Tag all resources with A=B", RejectionCode.UNSUPPORTED_REQUEST),             # no explicit type
    ("tag all with A=B", RejectionCode.AMBIGUOUS_REQUEST),                         # "all" of nothing
    ("Tag lambda function with A=B", RejectionCode.AMBIGUOUS_REQUEST),             # type but no name
    ("Tag ServiceWRK with Owner", RejectionCode.MALFORMED_TAGS),
    ("Tag ServiceWRK with A=1, a=2", RejectionCode.MALFORMED_TAGS),                # duplicate key by case
    ('Tag ServiceWRK with A="unbalanced', RejectionCode.MALFORMED_TAGS),
    ("Tag ServiceWRK in mars-north-9 with A=B", RejectionCode.UNSUPPORTED_REQUEST),  # bad qualifier
    ("Tag ServiceWRK in ap-south-1 in us-east-1 with A=B", RejectionCode.AMBIGUOUS_REQUEST),
    ("Update Owner of ServiceWRK to a, b", RejectionCode.MALFORMED_TAGS),
])
def test_rule_parser_rejects_instead_of_guessing(text, code):
    result = RuleBasedParser().parse(text)
    assert not isinstance(result, TaggingRequest)
    assert result is not None and result.code == code


@pytest.mark.parametrize("text", ["please make my account compliant", "what tags does ServiceWRK have?", "set Owner=x"])
def test_unrecognised_text_returns_none_so_a_fallback_can_try(text):
    assert RuleBasedParser().parse(text) is None
    assert prepare(text).rejection.code == RejectionCode.UNSUPPORTED_REQUEST


def test_fallback_parser_is_consulted_but_its_output_is_validated_server_side():
    class FakeModelParser:  # stands in for a future Bedrock parser
        name = "fake-model"

        def __init__(self, request):
            self.request = request
            self.calls = 0

        def parse(self, text):
            self.calls += 1
            return self.request

    bad = TaggingRequest(operation=TaggingOperation.ADD_TAG, target=TargetCriteria(name="ServiceWRK"),
                         tags={"aws:owner": "x"}, raw_text="x", parsed_by="fake-model")
    fallback = FakeModelParser(bad)
    out = engine(parser=CompositeParser(RuleBasedParser(), fallback)).prepare("make ServiceWRK owned by x")
    assert fallback.calls == 1
    assert out.status == OutcomeStatus.REJECTED and "reserved" in out.message


def test_rule_rejections_are_final_and_never_reach_the_fallback():
    class Boom:
        name = "boom"

        def parse(self, text):
            raise AssertionError("fallback must not be called")

    out = engine(parser=CompositeParser(RuleBasedParser(), Boom())).prepare("Remove Owner from ServiceWRK")
    assert out.status == OutcomeStatus.REJECTED


def test_request_model_rejects_inconsistent_shapes():
    with pytest.raises(ValidationError):  # bulk op on a specific target
        TaggingRequest(operation=TaggingOperation.BULK_ADD_TAG, target=TargetCriteria(name="x"),
                       tags={"A": "B"}, raw_text="", parsed_by="t")
    with pytest.raises(ValidationError):  # specific op on a bulk target
        TaggingRequest(operation=TaggingOperation.ADD_TAG,
                       target=TargetCriteria(resource_type="AWS::S3::Bucket", all_of_type=True),
                       tags={"A": "B"}, raw_text="", parsed_by="t")
    with pytest.raises(ValidationError):  # update changes exactly one tag
        TaggingRequest(operation=TaggingOperation.UPDATE_TAG, target=TargetCriteria(name="x"),
                       tags={"A": "1", "B": "2"}, raw_text="", parsed_by="t")
    with pytest.raises(ValidationError):  # bulk without type
        TargetCriteria(all_of_type=True)


# =========================================================================== specific targeting

def test_specific_target_by_name_affects_exactly_one_resource():
    out = prepare("Tag the Lambda function ServiceWRK with ServiceWRK=Lambda")
    assert out.status == OutcomeStatus.PROPOSAL_READY
    p = out.proposal
    assert [r.resource_name for r in p.records] == ["ServiceWRK"] and p.impact.resources_matched == 1
    r = p.records[0]
    assert r.outcome == ResourceOutcome.CHANGE_PROPOSED
    (change,) = r.changes
    assert (change.key, change.action, change.old_value, change.new_value) == ("ServiceWRK", ChangeAction.ADD, None, "Lambda")
    assert r.current_tags == {"Project": "CloudGuard", "Environment": "Development"}


def test_specific_target_never_expands_to_other_resources_of_the_type():
    out = prepare("Tag Lambda function image-resizer with Project=CloudGuard")
    assert [r.resource_name for r in out.proposal.records] == ["image-resizer"]
    assert out.proposal.impact.resources_out_of_scope == 0  # specific requests never list others


def test_specific_target_by_arn():
    out = prepare("Tag arn:aws:lambda:us-east-1:123456789012:function:worker with Team=edge")
    (only,) = out.proposal.records
    assert only.region == "us-east-1" and only.resource_arn.endswith(":function:worker")


def test_specific_target_by_type_and_region_disambiguates():
    out = prepare("tag lambda function worker in us-east-1 with Team=edge")
    assert out.status == OutcomeStatus.PROPOSAL_READY and out.proposal.records[0].region == "us-east-1"


@pytest.mark.parametrize(("text", "status"), [
    ("Tag Lambda function ghost with A=B", OutcomeStatus.NOT_FOUND),
    ("Tag arn:aws:lambda:ap-south-1:123456789012:function:ghost with A=B", OutcomeStatus.NOT_FOUND),
    ("Tag dynamodb table ServiceWRK with A=B", OutcomeStatus.NOT_FOUND),             # right name, wrong type
])
def test_unmatched_targets(text, status):
    out = prepare(text)
    assert out.status == status and out.proposal is None


@pytest.mark.parametrize("text", [
    "Tag ServiceWRK in account 999999999999 with A=B",
    "Tag ServiceWRK in eu-west-1 with A=B",                                           # region not discovered
])
def test_out_of_scope_requests_are_rejected(text):
    out = prepare(text)
    assert out.status == OutcomeStatus.REJECTED and out.rejection.code == RejectionCode.OUT_OF_SCOPE


def test_case_insensitive_name_is_a_clarification_not_a_guess():
    out = prepare("Tag servicewrk with A=B")
    assert out.status == OutcomeStatus.NEEDS_CLARIFICATION and out.proposal is None
    assert [c.resource_name for c in out.candidates] == ["ServiceWRK"]


# =========================================================================== ambiguity / duplicates

def test_ambiguous_name_across_types_asks_for_clarification():
    out = prepare("Tag shared-name with Team=x")
    assert out.status == OutcomeStatus.NEEDS_CLARIFICATION and out.proposal is None
    assert {c.resource_type for c in out.candidates} == {"AWS::Lambda::Function", "AWS::DynamoDB::Table"}
    assert prepare("Tag dynamodb table shared-name with Team=x").status == OutcomeStatus.PROPOSAL_READY


def test_ambiguous_name_across_regions_asks_for_clarification():
    out = prepare("Update Owner of worker to x")
    assert out.status == OutcomeStatus.NEEDS_CLARIFICATION
    assert sorted(c.region for c in out.candidates) == ["ap-south-1", "us-east-1"]


def test_duplicate_discovered_resources_are_counted_once():
    d = FixtureSource(FIXTURE).discover()
    duplicated = d.model_copy(update={"resources": d.resources + d.resources[:3]})
    single = engine(source=Static(duplicated)).prepare("Tag ServiceWRK with A=B")
    assert single.status == OutcomeStatus.PROPOSAL_READY and len(single.proposal.records) == 1
    bulk = engine(source=Static(duplicated)).prepare("Tag all lambda functions with A=B")
    arns = [r.resource_arn for r in bulk.proposal.records]
    assert len(arns) == len(set(arns)) == 7


# =========================================================================== bulk targeting

def test_bulk_add_lists_exact_resources_with_distinct_outcomes():
    out = prepare("Tag all Lambda functions with Project=CloudGuard")
    p = out.proposal
    assert out.status == OutcomeStatus.PROPOSAL_READY and p.operation == TaggingOperation.BULK_ADD_TAG
    lambdas = {r.resource_arn for r in FixtureSource(FIXTURE).discover().resources if r.resource_type == "AWS::Lambda::Function"}
    assert {r.resource_arn for r in p.records} == lambdas  # every Lambda, nothing else
    assert {r.resource_name for r in p.records_with(ResourceOutcome.CHANGE_PROPOSED)} == {"image-resizer", "shared-name", "worker"}
    assert {r.resource_name for r in p.records_with(ResourceOutcome.ALREADY_MATCHING)} == {"ServiceWRK", "report-worker"}
    (conflict,) = p.records_with(ResourceOutcome.CONFLICT)
    c = conflict.conflicts[0]
    assert (conflict.resource_name, c.reason, c.existing_value, c.requested_value) == \
        ("billing-api", ConflictReason.WOULD_OVERWRITE, "OtherProject", "CloudGuard")
    i = p.impact
    assert (i.resources_matched, i.resources_changing, i.resources_already_matching, i.resources_conflicting) == (7, 4, 2, 1)
    assert i.tags_overwritten == 0 and not p.requires_overwrite_confirmation


def test_bulk_region_filter_lists_excluded_resources_as_out_of_scope():
    out = prepare("tag all lambda functions in us-east-1 with Team=edge")
    p = out.proposal
    assert [(r.resource_name, r.region) for r in p.records_with(ResourceOutcome.CHANGE_PROPOSED)] == [("worker", "us-east-1")]
    oos = p.records_with(ResourceOutcome.OUT_OF_SCOPE)
    assert len(oos) == 6 and all(r.region == "ap-south-1" for r in oos)
    assert all(r.current_tags is None and not r.changes and "limited to us-east-1" in r.out_of_scope_reason for r in oos)
    assert (p.impact.resources_matched, p.impact.resources_out_of_scope, p.impact.resources_considered) == (1, 6, 7)
    assert p.is_approvable  # out-of-scope records never block


def test_bulk_never_includes_other_resource_types():
    out = prepare("Add Owner=data-team to all DynamoDB tables")
    assert {r.resource_type for r in out.proposal.records} == {"AWS::DynamoDB::Table"}


def test_bulk_update_overwrites_only_existing_keys_and_flags_them():
    out = prepare("Update Owner of all Lambda functions to platform-team")
    p = out.proposal
    changed = {r.resource_name: r.changes[0] for r in p.records_with(ResourceOutcome.CHANGE_PROPOSED)}
    assert set(changed) == {"billing-api", "worker"}
    assert all(c.action == ChangeAction.OVERWRITE and c.old_value for c in changed.values())
    assert p.requires_overwrite_confirmation and p.impact.tags_overwritten == 2
    assert rec(out, "image-resizer").conflicts[0].reason == ConflictReason.KEY_NOT_PRESENT  # never silently added
    assert "OVERWRITE" in out.message


def test_bulk_with_no_resources_of_type_is_not_found():
    d = FixtureSource(FIXTURE).discover()
    no_ec2 = d.model_copy(update={"resources": [r for r in d.resources if r.resource_type != "AWS::EC2::Instance"]})
    assert engine(source=Static(no_ec2)).prepare("Tag all EC2 instances with A=B").status == OutcomeStatus.NOT_FOUND


# =========================================================================== existing values / overwrites

def test_add_never_overwrites_a_different_existing_value():
    out = prepare("Tag Lambda function billing-api with Project=CloudGuard")
    assert out.status == OutcomeStatus.NO_CHANGES
    r = out.proposal.records[0]
    assert r.outcome == ResourceOutcome.CONFLICT
    assert r.conflicts[0].reason == ConflictReason.WOULD_OVERWRITE and r.conflicts[0].existing_value == "OtherProject"


def test_add_with_identical_value_is_already_matching():
    out = prepare("Tag ServiceWRK with Project=CloudGuard")
    assert out.status == OutcomeStatus.NO_CHANGES
    r = out.proposal.records[0]
    assert r.outcome == ResourceOutcome.ALREADY_MATCHING and r.matching[0].value == "CloudGuard"


def test_update_records_old_value_and_requires_confirmation():
    out = prepare("Update the Owner tag of Lambda function billing-api to BackendTeam")
    (c,) = out.proposal.records[0].changes
    assert (c.action, c.old_value, c.new_value) == (ChangeAction.OVERWRITE, "payments-team", "BackendTeam")
    assert out.proposal.requires_overwrite_confirmation


def test_update_of_absent_key_is_a_conflict_not_an_add():
    out = prepare("Update Owner of Lambda function image-resizer to x")
    assert out.status == OutcomeStatus.NO_CHANGES
    assert out.proposal.records[0].conflicts[0].reason == ConflictReason.KEY_NOT_PRESENT


@pytest.mark.parametrize("text", ["Tag bucket acme-logs with Project=new", "Update Project of bucket acme-logs to new"])
def test_case_variant_key_is_a_conflict_not_a_duplicate(text):
    out = prepare(text)
    r = out.proposal.records[0]
    assert r.outcome == ResourceOutcome.CONFLICT and r.changes == []
    c = r.conflicts[0]
    assert (c.reason, c.existing_key, c.existing_value) == (ConflictReason.KEY_CASE_CONFLICT, "project", "legacy")


def test_multiple_tags_mix_changes_and_matches_per_key():
    out = prepare("Tag ServiceWRK with Project=CloudGuard, Owner=core-team")
    r = out.proposal.records[0]
    assert r.outcome == ResourceOutcome.CHANGE_PROPOSED
    assert [(c.key, c.action) for c in r.changes] == [("Owner", ChangeAction.ADD)]
    assert [m.key for m in r.matching] == ["Project"]


def test_partial_conflicts_are_counted_separately():
    out = prepare("Tag billing-api with Project=CloudGuard, Team=payments")
    r = out.proposal.records[0]
    assert r.outcome == ResourceOutcome.CHANGE_PROPOSED and r.conflicts[0].reason == ConflictReason.WOULD_OVERWRITE
    assert out.proposal.impact.resources_changing_with_partial_conflicts == 1
    assert out.proposal.impact.tag_conflicts == 1 and out.proposal.impact.conflicts_by_reason == {"WOULD_OVERWRITE": 1}


def test_existing_tags_are_preserved_exactly():
    source = FixtureSource(FIXTURE)
    before = {r.resource_arn: dict(r.tags) for r in source.discover().resources if r.tags is not None}
    out = engine(source=source).prepare("Tag all Lambda functions with Project=CloudGuard")
    for r in out.proposal.records:
        assert r.current_tags == before[r.resource_arn]
    after = {r.resource_arn: dict(r.tags) for r in source.discover().resources if r.tags is not None}
    assert after == before  # nothing was modified


def test_uninspectable_resource_is_an_inspection_failure_not_a_guess():
    out = prepare("Tag bucket acme-locked with Owner=x")
    assert out.status == OutcomeStatus.NO_CHANGES
    (r,) = out.proposal.records
    assert r.outcome == ResourceOutcome.INSPECTION_FAILED and r.inspection_error.code == "AccessDenied"
    assert r.current_tags is None and not r.changes
    assert "could not be inspected" in out.message


def test_bulk_lists_inspection_failures_separately():
    out = prepare("Tag all S3 buckets with Owner=storage-team")
    p = out.proposal
    assert [r.resource_name for r in p.records_with(ResourceOutcome.CHANGE_PROPOSED)] == ["acme-logs"]
    assert [r.resource_name for r in p.records_with(ResourceOutcome.INSPECTION_FAILED)] == ["acme-locked"]
    assert p.is_approvable  # inspection failures are excluded, not blocking


def test_tag_limit_is_a_conflict_per_tag():
    d = FixtureSource(FIXTURE).discover()
    full = {f"k{i}": "v" for i in range(50)}
    crowded = [r.model_copy(update={"tags": {**full, "aws:cloudformation:stack-name": "s"}}) if r.resource_name == "image-resizer" else r
               for r in d.resources]
    out = engine(source=Static(d.model_copy(update={"resources": crowded}))).prepare("Tag image-resizer with Team=x")
    r = out.proposal.records[0]
    assert r.outcome == ResourceOutcome.CONFLICT and r.conflicts[0].reason == ConflictReason.TAG_LIMIT_EXCEEDED
    assert "50 existing + 1 new" in r.conflicts[0].detail  # aws: tags don't count


# =========================================================================== empty values / invalid keys

@pytest.mark.parametrize(("text", "fragment"), [
    ("Tag ServiceWRK with Owner=", "empty value"),
    ('Tag ServiceWRK with Owner=""', "empty value"),
    ('Tag ServiceWRK with Owner="   "', "empty value"),
    ("Tag ServiceWRK with aws:owner=x", "reserved"),
    ("Tag ServiceWRK with AWS:Owner=x", "reserved"),
    ("Tag ServiceWRK with " + "K" * 129 + "=x", "exceeds 128"),
    ("Tag ServiceWRK with Owner=" + "v" * 257, "exceeds 256"),
    ("Tag ServiceWRK with Own#er=x", "characters AWS doesn't allow"),
    ("Tag ServiceWRK with Owner=a;b", "characters AWS doesn't allow"),
    ('Tag ServiceWRK with " Owner"=x', "leading or trailing spaces"),
])
def test_invalid_keys_and_values_are_rejected(text, fragment):
    out = prepare(text)
    assert out.status == OutcomeStatus.REJECTED and out.rejection.code == RejectionCode.INVALID_TAG
    assert fragment in out.message and out.proposal is None


def test_unicode_and_allowed_punctuation_are_accepted():
    out = prepare('Tag ServiceWRK with "team:é/owner"="ops@example.com + 1"')
    assert out.status == OutcomeStatus.PROPOSAL_READY


# =========================================================================== policy interactions

def test_policy_violation_is_a_per_resource_outcome_that_blocks_approval():
    out = prepare("Add Environment=qa to all DynamoDB tables")
    assert out.status == OutcomeStatus.BLOCKED and out.rejection is None
    p = out.proposal
    assert p.approval_status == ApprovalStatus.NOT_APPROVABLE and not p.is_approvable
    assert any("violate the tagging policy" in b for b in p.blocking_reasons)
    assert p.impact.resources_policy_violations == 3 and p.impact.proposed_changes == []
    v = p.records[0].policy_violations[0]
    assert (v.key, v.value, v.policy_key) == ("Environment", "qa", "Environment")
    assert v.allowed_values == ["Development", "Staging", "Production"] and "prod" in v.aliases


def test_one_violating_tag_blocks_the_whole_request_and_proposes_nothing():
    out = prepare("Tag image-resizer with Environment=qa, Team=imaging")
    r = out.proposal.records[0]
    assert out.status == OutcomeStatus.BLOCKED and r.outcome == ResourceOutcome.POLICY_VIOLATION
    assert r.changes == [] and out.proposal.impact.proposed_changes == []


def test_policy_alias_is_accepted_but_not_rewritten():
    out = prepare("Tag image-resizer with Environment=prod")
    (c,) = out.proposal.records[0].changes
    assert c.new_value == "prod" and c.category == TagCategory.POLICY
    assert any("canonical value is 'Production'" in n for n in c.notes)


def test_policy_key_spelling_is_noted():
    out = prepare("Tag image-resizer with environment=Staging")
    assert any("policy key is spelled 'Environment'" in n for n in out.proposal.records[0].changes[0].notes)


def test_custom_tag_is_categorised_custom_and_never_enters_the_policy():
    pol = policy()
    out = engine(pol=pol).prepare("Tag ServiceWRK with ServiceWRK=Lambda")
    (c,) = out.proposal.records[0].changes
    assert c.category == TagCategory.CUSTOM and "not part of the mandatory tagging policy" in c.notes[0]
    assert "ServiceWRK" not in [r.key for r in pol.document.rules_for("AWS::Lambda::Function")]
    assert out.proposal.policy == pol.ref
    assert "ServiceWRK" not in out.proposal.records[0].projected_missing_tags


def test_projected_compliance_reflects_mandatory_policy_only():
    out = prepare("Tag image-resizer with Environment=Development, Project=p, Owner=o, CostCenter=c")
    assert out.proposal.records[0].projected_status == ComplianceStatus.COMPLIANT
    custom = prepare("Tag image-resizer with ServiceWRK=Lambda")
    assert custom.proposal.records[0].projected_status == ComplianceStatus.NON_COMPLIANT
    assert custom.proposal.records[0].projected_missing_tags == ["Environment", "Project", "Owner", "CostCenter"]


def test_per_type_policy_overrides_apply_to_the_targeted_type():
    data = DEFAULT_POLICY_DOCUMENT.model_dump()
    data["overrides"] = {"AWS::S3::Bucket": {"rules": [{"key": "DataClass", "allowed_values": ["Public", "Internal"]}]}}
    pol = policy(parse_policy(data))
    bad = engine(pol=pol).prepare("Tag bucket acme-logs with DataClass=Secret")
    assert bad.status == OutcomeStatus.BLOCKED
    ok_on_lambda = engine(pol=pol).prepare("Tag ServiceWRK with DataClass=Secret")  # custom for Lambda
    assert ok_on_lambda.proposal.records[0].changes[0].category == TagCategory.CUSTOM


# =========================================================================== impact summary

ALL_OUTCOME_REQUESTS = [
    "Tag all Lambda functions with Project=CloudGuard",
    "tag all lambda functions in us-east-1 with Team=edge",
    "Add Environment=qa to all DynamoDB tables",
    "Tag all S3 buckets with Owner=storage-team",
    "Update Owner of all Lambda functions to platform-team",
    "Tag billing-api with Project=CloudGuard, Team=payments",
]


@pytest.mark.parametrize("text", ALL_OUTCOME_REQUESTS)
def test_impact_summary_is_derived_from_records(text):
    p = prepare(text).proposal
    assert p.impact == summarize_records(p.records)
    by = p.impact.resource_arns_by_outcome
    assert set(by) == {o.value for o in ResourceOutcome}  # every category present, even if empty
    assert sorted(a for arns in by.values() for a in arns) == sorted(r.resource_arn for r in p.records)
    assert [(c.resource_arn, c.key, c.action, c.old_value, c.new_value) for c in p.impact.proposed_changes] == \
        [(r.resource_arn, c.key, c.action, c.old_value, c.new_value) for r in p.records for c in r.changes]


def test_all_six_outcomes_are_distinct_across_the_fixture():
    seen = {r.outcome for t in ALL_OUTCOME_REQUESTS for r in prepare(t).proposal.records}
    assert seen == set(ResourceOutcome)


def test_impact_summary_cannot_disagree_with_records():
    p = prepare("Tag all Lambda functions with Project=CloudGuard").proposal
    data = p.model_dump(mode="json")
    data["impact"]["resources_changing"] = 1
    data["content_hash"] = proposal_hash(data)  # even with a recomputed hash
    with pytest.raises(ValidationError, match="derived from the records"):
        TaggingProposal.model_validate(data)


@pytest.mark.parametrize(("outcome", "fields"), [
    (ResourceOutcome.CHANGE_PROPOSED, {}),                                               # no changes
    (ResourceOutcome.ALREADY_MATCHING, {"conflicts": [{"key": "A", "reason": "WOULD_OVERWRITE", "requested_value": "x"}]}),
    (ResourceOutcome.CONFLICT, {}),
    (ResourceOutcome.POLICY_VIOLATION, {}),
    (ResourceOutcome.INSPECTION_FAILED, {"current_tags": {}}),
    (ResourceOutcome.OUT_OF_SCOPE, {"current_tags": {}}),
])
def test_record_outcome_must_match_its_details(outcome, fields):
    base = dict(outcome=outcome, resource_type="AWS::Lambda::Function", resource_name="f", resource_arn="arn:f",
                account_id=ACCOUNT, region="ap-south-1", current_tags={}, tags_inspected_at=NOW)
    if outcome == ResourceOutcome.ALREADY_MATCHING:
        base["matching"] = [{"key": "B", "value": "y"}]
    with pytest.raises(ValidationError):
        ResourceRecord.model_validate({**base, **fields})


# =========================================================================== proposal integrity / approval readiness

def test_proposal_hash_binds_content_and_detects_tampering():
    p = prepare("Tag all Lambda functions with Project=CloudGuard").proposal
    assert p.content_hash == proposal_hash(p) and p.content_hash.startswith("sha256:")
    data = p.model_dump(mode="json")
    idx = next(i for i, r in enumerate(data["records"]) if r["changes"])
    data["records"][idx]["changes"][0]["new_value"] = "Evil"  # swap the approved value
    with pytest.raises(ValidationError, match="content_hash"):
        TaggingProposal.model_validate(data)
    data2 = p.model_dump(mode="json")
    data2["records"][3]["current_tags"]["Injected"] = "x"
    with pytest.raises(ValidationError, match="content_hash"):
        TaggingProposal.model_validate(data2)


def test_approval_status_is_hashed_and_cannot_be_flipped():
    p = prepare("Tag ServiceWRK with Project=CloudGuard").proposal  # NO_CHANGES
    assert p.approval_status == ApprovalStatus.NOT_APPROVABLE and p.blocking_reasons == ["no tag would change"]
    data = p.model_dump(mode="json")
    data["approval_status"] = "PENDING"
    with pytest.raises(ValidationError, match="content_hash"):
        TaggingProposal.model_validate(data)
    data["content_hash"] = proposal_hash(data)
    with pytest.raises(ValidationError, match="approvable"):
        TaggingProposal.model_validate(data)


def test_proposal_invariants_cannot_be_forged_even_with_a_matching_hash():
    p = prepare("Tag ServiceWRK with A=B").proposal
    data = p.model_dump(mode="json")
    data["records"][0]["changes"][0].update({"action": "OVERWRITE", "old_value": "x"})
    data["impact"] = summarize_records([ResourceRecord.model_validate(r) for r in data["records"]]).model_dump(mode="json")
    data["requires_overwrite_confirmation"] = True
    data["content_hash"] = proposal_hash(data)  # attacker recomputes everything derivable
    with pytest.raises(ValidationError, match="add operations can never overwrite"):
        TaggingProposal.model_validate(data)


def test_proposal_expiry_bounds_and_unique_ids():
    e = engine(proposal_ttl=timedelta(minutes=5))
    a = e.prepare("Tag ServiceWRK with A=B").proposal
    b = e.prepare("Tag ServiceWRK with A=B").proposal
    assert a.expires_at - a.created_at == timedelta(minutes=5)
    assert a.proposal_id != b.proposal_id and a.content_hash != b.content_hash
    with pytest.raises(ValidationError, match="expires_at"):
        engine(proposal_ttl=timedelta(hours=2)).prepare("Tag ServiceWRK with A=B")
    with pytest.raises(ValidationError, match="expires_at"):
        engine(proposal_ttl=timedelta(0)).prepare("Tag ServiceWRK with A=B")


def test_unknown_schema_version_is_rejected():
    data = prepare("Tag ServiceWRK with A=B").proposal.model_dump(mode="json")
    data["schema_version"] = "cloudguard.tagging-proposal/v0"
    data["content_hash"] = proposal_hash(data)
    with pytest.raises(ValidationError, match="schema"):
        TaggingProposal.model_validate(data)


def test_approval_binding_lists_expected_state_for_revalidation():
    out = prepare("Update Owner of all Lambda functions to platform-team")
    b = out.proposal.approval_binding()
    assert b["proposal_id"] == out.proposal.proposal_id and b["content_hash"] == out.proposal.content_hash
    assert b["account_id"] == ACCOUNT and b["policy"]["version"] == 1 and b["requires_overwrite_confirmation"] is True
    assert sorted((e["key"], e["expected_current_value"], e["new_value"]) for e in b["expected_state"]) == [
        ("Owner", "edge-team", "platform-team"), ("Owner", "payments-team", "platform-team")]
    added = prepare("Tag ServiceWRK with A=B").proposal.approval_binding()
    assert added["expected_state"][0]["expected_current_value"] is None  # must still be absent at execution


def test_each_request_re_inspects_current_tags():
    src = Static(FixtureSource(FIXTURE).discover())
    e = engine(source=src)
    e.prepare("Tag ServiceWRK with A=B")
    e.prepare("Tag ServiceWRK with A=B")
    assert src.calls == 2


# =========================================================================== live mode: read-only

class FakeSession:
    def __init__(self):
        self.clients = {}

    def client(self, service, region_name, config=None):
        key = (service, region_name)
        if key not in self.clients:
            self.clients[key] = boto3.client(service, region_name=region_name, aws_access_key_id="t", aws_secret_access_key="t")
        return self.clients[key]

    def get_partition_for_region(self, region):
        return "aws"


def test_live_mode_only_reads_and_never_calls_write_apis():
    session = FakeSession()
    stubs = []
    sts = Stubber(session.client("sts", "ap-south-1"))
    sts.add_response("get_caller_identity", {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/u", "UserId": "u"})
    lam = Stubber(session.client("lambda", "ap-south-1"))
    arn = f"arn:aws:lambda:ap-south-1:{ACCOUNT}:function:svc"
    lam.add_response("list_functions", {"Functions": [{"FunctionName": "svc", "FunctionArn": arn}]})
    lam.add_response("list_tags", {"Tags": {"Owner": "a"}}, {"Resource": arn})
    ddb = Stubber(session.client("dynamodb", "ap-south-1"))
    ddb.add_response("list_tables", {"TableNames": []})
    s3 = Stubber(session.client("s3", "ap-south-1"))
    s3.add_response("list_buckets", {"Buckets": [], "Owner": {"ID": "o"}}, {"BucketRegion": "ap-south-1"})
    ec2 = Stubber(session.client("ec2", "ap-south-1"))
    ec2.add_response("describe_instances", {"Reservations": []})
    stubs += [sts, lam, ddb, s3, ec2]
    for s in stubs:
        s.activate()
    try:
        # Any tag_resource / put_bucket_tagging / create_tags call would fail: no stubbed response.
        out = CustomTaggingEngine(LiveSource(session, ["ap-south-1"]), policy()).prepare("Update Owner of lambda function svc to b")
    finally:
        for s in stubs:
            s.deactivate()
    for s in stubs:
        s.assert_no_pending_responses()
    assert out.status == OutcomeStatus.PROPOSAL_READY
    assert out.proposal.records[0].changes[0].old_value == "a"


# =========================================================================== CLI

def test_cli_tag_command_prints_proposal_and_exit_codes(tmp_path, capsys):
    store = ["--policy-store", str(tmp_path), "--fixture", str(FIXTURE)]
    assert main(["tag", "Tag the Lambda function ServiceWRK with ServiceWRK=Lambda", *store]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "PROPOSAL_READY" and out["proposal"]["impact"]["tags_added"] == 1
    assert main(["tag", "Add Environment=qa to all DynamoDB tables", *store]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert main(["tag", "Tag shared-name with A=B", *store]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "NEEDS_CLARIFICATION"
    assert main(["tag", "Remove Owner from ServiceWRK", *store]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "REJECTED"


def test_oversized_requests_are_rejected_before_parsing():
    from cloudguard.governance.custom_tagging.parser import MAX_REQUEST_CHARS

    ok = "Tag ServiceWRK with A=" + "b" * 200
    assert isinstance(RuleBasedParser().parse(ok), TaggingRequest)
    out = prepare("Tag ServiceWRK with A=" + "b" * MAX_REQUEST_CHARS)
    assert out.status == OutcomeStatus.REJECTED and "exceeds" in out.message
