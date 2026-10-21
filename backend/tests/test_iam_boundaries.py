"""H2: IAM privilege boundaries in infra/cloudguard-core.yaml and infra/iam/.

These are STRUCTURAL assertions about the policy documents and about the code's DynamoDB call
sites. They do not prove AWS IAM or CloudFormation runtime behaviour: Access Analyzer, the IAM
policy simulator and a sandbox deployment are still required before any deployment.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = ROOT / "infra" / "cloudguard-core.yaml"
IAM_DIR = ROOT / "infra" / "iam"
CODE = {name: (ROOT / "backend" / "cloudguard" / "governance" / path).read_text(encoding="utf-8") for name, path in {
    "policy_store": "policy_store.py", "authz": "remediation/authz.py", "repository": "remediation/repository.py"}.items()}

PREFIXES = {"PROPOSAL#", "IDEMP#", "NONCE#", "AUTHZ#", "POLICY#"}
TAG_MUTATIONS = {"lambda:TagResource", "lambda:UntagResource", "dynamodb:TagResource", "dynamodb:UntagResource",
                 "ec2:CreateTags", "ec2:DeleteTags", "s3:PutBucketTagging", "tag:TagResources", "tag:UntagResources"}
TABLE_MUTATIONS = {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem",
                   "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete"}
NEVER_ALLOWED = {"dynamodb:BatchWriteItem", "dynamodb:DeleteItem", "dynamodb:Scan", "dynamodb:ConditionCheckItem",
                 "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete", "dynamodb:PartiQLSelect",
                 "dynamodb:TransactWriteItems", "dynamodb:*", "*"}
TABLE_DATA_ACTIONS = {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                      "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem",
                      "dynamodb:ConditionCheckItem"}


class _CfnLoader(yaml.SafeLoader):
    """Parses CloudFormation short-form intrinsics (!Ref, !GetAtt, !Sub, ...) into their long form."""


def _intrinsic(loader, suffix, node):
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        if suffix == "GetAtt":
            value = value.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {name: value}


_CfnLoader.add_multi_constructor("!", _intrinsic)
TEMPLATE = yaml.load(TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CfnLoader)  # noqa: S506 - safe subclass
RESOURCES = TEMPLATE["Resources"]


def as_list(v):
    return v if isinstance(v, list) else [v]


def statements(document):
    return document["Statement"]


def inspector_statements():
    role = RESOURCES["InspectorRole"]["Properties"]
    return [s for p in role["Policies"] for s in statements(p["PolicyDocument"])]


def managed(name):
    return {s["Sid"]: s for s in statements(RESOURCES[name]["Properties"]["PolicyDocument"])}


def all_template_statements():
    out = []
    for res in RESOURCES.values():
        props = res.get("Properties", {})
        for p in props.get("Policies", []):
            out += statements(p["PolicyDocument"])
        if "PolicyDocument" in props:
            out += statements(props["PolicyDocument"])
    return out


def allowed_actions(stmts):
    return {a for s in stmts if s["Effect"] == "Allow" for a in as_list(s["Action"])}


def is_table_arn(resource):
    return resource == {"Fn::GetAtt": ["ScanTable", "Arn"]}


# ================================================================ inspector

def test_inspector_has_no_tagging_and_explicitly_denies_it():
    stmts = inspector_statements()
    assert not TAG_MUTATIONS & allowed_actions(stmts)
    deny = next(s for s in stmts if s.get("Sid") == "InspectorNeverTagsSupportedResources")
    assert deny["Effect"] == "Deny" and set(deny["Action"]) == TAG_MUTATIONS and deny["Resource"] == "*"
    assert "Condition" not in deny


def test_inspector_cannot_mutate_governance_records():
    stmts = inspector_statements()
    assert not {a for a in allowed_actions(stmts) if a in TABLE_DATA_ACTIONS}  # no table data access at all
    deny = next(s for s in stmts if s.get("Sid") == "InspectorNeverWritesGovernanceTable")
    assert deny["Effect"] == "Deny" and set(deny["Action"]) == TABLE_MUTATIONS and "Condition" not in deny
    assert {"Fn::GetAtt": ["ScanTable", "Arn"]} in deny["Resource"]
    assert {"Fn::Sub": "${ScanTable.Arn}/index/*"} in deny["Resource"]


def test_inspector_trust_is_restricted_to_named_principals_of_this_account():
    trust = RESOURCES["InspectorRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    stmt = trust[0]
    assert stmt["Effect"] == "Allow" and stmt["Action"] == "sts:AssumeRole"
    assert stmt["Principal"] == {"AWS": {"Ref": "InspectorTrustedPrincipalArns"}}
    assert stmt["Condition"] == {"StringEquals": {"aws:PrincipalAccount": {"Ref": "AWS::AccountId"}}}
    assert ":root" not in json.dumps(TEMPLATE)


@pytest.mark.parametrize(("value", "ok"), [
    ("arn:aws:iam::123456789012:role/Ops", True),
    ("arn:aws:iam::123456789012:user/team/alice", True),
    ("arn:aws:iam::123456789012:role/Ops,arn:aws:iam::123456789012:user/alice", True),
    ("*", False),
    ("arn:aws:iam::123456789012:root", False),
    ("arn:aws:iam::123456789012:role/*", False),
    ("arn:aws:iam::*:role/Ops", False),
    ("arn:aws:iam::123456789012:role/Ops,*", False),
    ("", False),
])
def test_trusted_principal_parameter_pattern(value, ok):
    param = TEMPLATE["Parameters"]["InspectorTrustedPrincipalArns"]
    assert param["Type"] == "CommaDelimitedList" and "Default" not in param
    pattern = re.compile(param["AllowedPattern"])
    whole = bool(pattern.fullmatch(value))  # whether CFN applies it to the whole string ...
    items = bool(value) and all(pattern.fullmatch(v) for v in value.split(","))  # ... or to each item
    assert whole == ok and items == ok


# ================================================================ no forbidden or remediation grants

def _infra_iam_statements():
    for path in sorted(IAM_DIR.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for s in doc["Statement"]:
            yield path.name, s


def test_no_batch_delete_scan_partiql_or_conditioncheck_is_ever_allowed():
    assert not NEVER_ALLOWED & allowed_actions(all_template_statements())
    for name, s in _infra_iam_statements():
        if s["Effect"] == "Allow":
            assert not NEVER_ALLOWED & set(as_list(s["Action"])), name


def test_remediation_actions_are_granted_nowhere_in_the_template():
    assert not TAG_MUTATIONS & allowed_actions(all_template_statements())
    assert "TaggerRole" not in json.dumps(RESOURCES)


def test_proposed_tagger_has_no_governance_table_access():
    tagger = json.loads((IAM_DIR / "proposed" / "tagger-role-policy.json").read_text(encoding="utf-8"))
    assert not TABLE_DATA_ACTIONS & allowed_actions(tagger["Statement"])


# ================================================================ governance-store policies

def _leading_key_prefixes(stmt):
    cond = stmt["Condition"]
    assert cond["Null"]["dynamodb:LeadingKeys"] == "false", stmt["Sid"]  # key must be present
    assert "ForAnyValue:StringLike" not in cond and "ForAnyValue:StringEquals" not in cond, stmt["Sid"]
    return {p.rstrip("*") for p in cond["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]}


@pytest.mark.parametrize("policy", ["GovernanceParticipantPolicy", "GovernanceAdminPolicy"])
def test_every_table_statement_is_allow_only_table_arn_and_leading_keys_scoped(policy):
    res = RESOURCES[policy]
    assert res["Type"] == "AWS::IAM::ManagedPolicy"
    assert not {"Roles", "Users", "Groups"} & set(res["Properties"])  # unattached: attachment is an account decision
    for sid, s in managed(policy).items():
        assert s["Effect"] == "Allow" and is_table_arn(s["Resource"]), sid  # no index ARNs: there are no indexes
        assert _leading_key_prefixes(s) <= PREFIXES, sid
        enclosing = json.dumps(s["Condition"])
        assert "ForAnyValue" not in enclosing  # EnclosingOperation is single-valued; LeadingKeys needs ForAllValues
        if "dynamodb:Query" in as_list(s["Action"]):
            assert "EnclosingOperation" not in enclosing, sid  # Query doesn't support that key


def test_participant_policy_never_writes_allow_list_or_policies():
    p = managed("GovernanceParticipantPolicy")
    writes = [s for s in p.values() if {"dynamodb:PutItem", "dynamodb:UpdateItem"} & set(as_list(s["Action"]))]
    assert {s["Sid"] for s in writes} == {"P1ParticipantTransactionalWrites", "P2ParticipantPlainProposalWrites"}
    for s in writes:
        assert not {"AUTHZ#", "POLICY#"} & _leading_key_prefixes(s)
    p1, p2 = p["P1ParticipantTransactionalWrites"], p["P2ParticipantPlainProposalWrites"]
    assert p1["Condition"]["StringEquals"] == {"dynamodb:EnclosingOperation": "TransactWriteItems"}
    assert _leading_key_prefixes(p1) == {"PROPOSAL#", "IDEMP#", "NONCE#"}
    assert p2["Condition"]["Null"] == {"dynamodb:EnclosingOperation": "true", "dynamodb:LeadingKeys": "false"}
    assert _leading_key_prefixes(p2) == {"PROPOSAL#"}  # plain (non-transactional) writes: proposal partitions only
    assert as_list(p["P3ParticipantItemReads"]["Action"]) == ["dynamodb:GetItem"]
    assert _leading_key_prefixes(p["P4ParticipantProposalQueries"]) == {"PROPOSAL#"}


def test_admin_policy_is_transactional_put_on_allow_list_and_policies_only():
    a = managed("GovernanceAdminPolicy")
    assert set(a) == {"A1AdminTransactionalVersionWrites", "A2AdminReads"}
    a1, a2 = a["A1AdminTransactionalVersionWrites"], a["A2AdminReads"]
    assert as_list(a1["Action"]) == ["dynamodb:PutItem"]  # no UpdateItem: both stores commit with two Puts
    assert a1["Condition"]["StringEquals"] == {"dynamodb:EnclosingOperation": "TransactWriteItems"}
    assert _leading_key_prefixes(a1) == _leading_key_prefixes(a2) == {"AUTHZ#", "POLICY#"}
    assert set(as_list(a2["Action"])) == {"dynamodb:GetItem", "dynamodb:Query"}
    assert not TAG_MUTATIONS & allowed_actions(a.values())


# ================================================================ TTL (Option D)

def test_governance_table_has_no_ttl():
    """Writers can set arbitrary attributes, so a table TTL would let them delete records."""
    table = RESOURCES["ScanTable"]["Properties"]
    assert "TimeToLiveSpecification" not in table
    assert not any("TimeToLiveSpecification" in r.get("Properties", {}) for r in RESOURCES.values())


def test_no_policy_grants_update_time_to_live():
    granted = allowed_actions(all_template_statements())
    assert not {"dynamodb:UpdateTimeToLive", "dynamodb:*", "*"} & granted
    for name, s in _infra_iam_statements():
        if s["Effect"] == "Allow":
            assert "dynamodb:UpdateTimeToLive" not in as_list(s["Action"]), name


# ================================================================ code <-> policy drift

def _transaction_entry_types():
    found = set()
    for src in CODE.values():
        found |= set(re.findall(r'\{"(Put|Update|Delete|ConditionCheck|Get)": \{', src))
        if "_head_update(" in src or "_fenced_lease_update(" in src:
            found.add("Update")
    return found


def test_code_uses_only_put_and_update_transaction_entries_and_no_indexes():
    assert _transaction_entry_types() == {"Put", "Update"}
    for name, src in CODE.items():
        for forbidden in ("IndexName", "batch_write_item", "batch_get_item", ".scan(", "delete_item",
                          "execute_statement", "transact_get_items", "ConditionCheck"):
            assert forbidden not in src, (name, forbidden)
    assert not any(k in TEMPLATE_PATH.read_text(encoding="utf-8")
                   for k in ("GlobalSecondaryIndexes", "LocalSecondaryIndexes"))


def test_plain_writes_only_target_proposal_partitions():
    plain = {}
    for name, src in CODE.items():
        func = None
        for line in src.splitlines():
            m = re.match(r"\s+def (\w+)\(", line)
            if m:
                func = m.group(1)
            if ".put_item(" in line or ".update_item(" in line:
                plain.setdefault(name, set()).add(func)
    assert plain == {"repository": {"repair_head", "take_over_lease"}}  # both key items with _pk() -> PROPOSAL#


def test_partition_prefixes_in_code_match_the_policies():
    in_code = set()
    for src in CODE.values():
        in_code |= {f"{p}#" for p in re.findall(r'"PK": \{"S": f"([A-Z]+)#', src)}
        in_code |= {f"{p}#" for p in re.findall(r'pk = \{"S": f"([A-Z]+)#', src)}
        in_code |= {f"{p}#" for p in re.findall(r'def _pk\([^)]*\)[^\n]*\n\s+return f"([A-Z]+)#', src)}
    assert in_code == PREFIXES
    p, a = managed("GovernanceParticipantPolicy"), managed("GovernanceAdminPolicy")
    covered = set().union(*(_leading_key_prefixes(s) for s in [*p.values(), *a.values()]))
    assert covered == PREFIXES


# ================================================================ template structure (not cfn-lint)

PSEUDO = {"AWS::AccountId", "AWS::Region", "AWS::Partition", "AWS::StackName", "AWS::StackId", "AWS::URLSuffix",
          "AWS::NoValue", "AWS::NotificationARNs"}


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_template_structure_and_references_resolve():
    """Structural check only - NOT a substitute for cfn-lint or `cloudformation validate-template`."""
    assert TEMPLATE["AWSTemplateFormatVersion"] == "2010-09-09"
    params, conditions = set(TEMPLATE["Parameters"]), set(TEMPLATE.get("Conditions", {}))
    names = set(RESOURCES)
    for name, res in RESOURCES.items():
        assert re.fullmatch(r"AWS::\w+::\w+", res["Type"]), name
        if "Condition" in res:
            assert res["Condition"] in conditions, name
    for node in _walk(TEMPLATE):
        if "Ref" in node:
            assert node["Ref"] in params | names | PSEUDO, node
        if "Fn::GetAtt" in node:
            assert node["Fn::GetAtt"][0] in names, node
        if "Fn::Sub" in node and isinstance(node["Fn::Sub"], str):
            for var in re.findall(r"\$\{([^}!]+)\}", node["Fn::Sub"]):
                assert var.split(".")[0] in params | names | PSEUDO, var
    for out in TEMPLATE["Outputs"].values():
        assert out["Value"]
    assert {"InspectorRole", "ScanTable", "GovernanceParticipantPolicy", "GovernanceAdminPolicy"} <= names
