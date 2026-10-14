import pytest

from cloudguard.governance.policy import (
    DEFAULT_POLICY_DOCUMENT,
    PolicyDocument,
    PolicyValidationError,
    TagRule,
    parse_policy,
)

BASE = {
    "defaults": [
        {"key": "Environment", "allowed_values": ["Development", "Staging", "Production"],
         "aliases": {"dev": "Development", "prod": "production"}},
        {"key": "Project"},
        {"key": "Owner"},
        {"key": "CostCenter"},
    ]
}


def keys(rules):
    return [r.key for r in rules]


def with_overrides(overrides):
    return {**BASE, "overrides": overrides}


def errors_for(data):
    with pytest.raises(PolicyValidationError) as exc:
        parse_policy(data)
    return " | ".join(exc.value.errors)


# ---------- defaults ----------

def test_default_policy_matches_agreed_decisions():
    env = DEFAULT_POLICY_DOCUMENT.rules_for("AWS::Lambda::Function")[0]
    assert env.key == "Environment"
    assert env.allowed_values == ["Development", "Staging", "Production"]
    assert env.aliases == {"dev": "Development", "stage": "Staging", "prod": "Production"}
    assert keys(DEFAULT_POLICY_DOCUMENT.defaults) == ["Environment", "Project", "Owner", "CostCenter"]


# ---------- inheritance and overrides ----------

def test_types_without_override_inherit_defaults():
    doc = parse_policy(with_overrides({"AWS::S3::Bucket": {"remove": ["CostCenter"]}}))
    for rtype in ("AWS::Lambda::Function", "AWS::DynamoDB::Table", "AWS::EC2::Instance"):
        assert keys(doc.rules_for(rtype)) == ["Environment", "Project", "Owner", "CostCenter"]


def test_override_can_add_replace_and_remove_rules():
    doc = parse_policy(with_overrides({
        "AWS::EC2::Instance": {
            "rules": [
                {"key": "owner", "description": "EC2 owner must be a team", "allowed_values": ["platform", "data"]},
                {"key": "PatchGroup"},
            ],
            "remove": ["CostCenter"],
        }
    }))
    ec2 = doc.rules_for("AWS::EC2::Instance")
    assert keys(ec2) == ["Environment", "Project", "owner", "PatchGroup"]  # replaced in place, added at end
    assert ec2[2].allowed_values == ["platform", "data"]
    assert keys(doc.rules_for("AWS::Lambda::Function")) == ["Environment", "Project", "Owner", "CostCenter"]


def test_override_rules_do_not_leak_between_types():
    doc = parse_policy(with_overrides({"AWS::DynamoDB::Table": {"rules": [{"key": "DataClassification"}]}}))
    assert "DataClassification" in keys(doc.rules_for("AWS::DynamoDB::Table"))
    assert "DataClassification" not in keys(doc.rules_for("AWS::S3::Bucket"))


# ---------- aliases ----------

def test_alias_targets_are_normalized_to_canonical_spelling():
    doc = parse_policy(BASE)
    assert doc.defaults[0].aliases == {"dev": "Development", "prod": "Production"}


@pytest.mark.parametrize(("value", "expected"), [
    ("Production", "Production"), ("PRODUCTION", "Production"), ("prod", "Production"),
    ("Prod", "Production"), (" dev ", "Development"), ("qa", None), ("", None),
])
def test_canonical_value(value, expected):
    assert parse_policy(BASE).defaults[0].canonical_value(value) == expected


def test_rule_without_allow_list_accepts_any_value():
    assert TagRule(key="Owner").canonical_value("anything") == "anything"


# ---------- validation failures ----------

@pytest.mark.parametrize(("data", "fragment"), [
    ({"defaults": []}, "at least one tag rule"),
    ({"defaults": [{"key": "Owner"}, {"key": "owner"}]}, "duplicate default rule keys"),
    ({"defaults": [{"key": "  "}]}, "must not be blank"),
    ({"defaults": [{"key": "aws:createdBy"}]}, "reserved by AWS"),
    ({"defaults": [{"key": "K" * 129}]}, "exceeds 128"),
    ({"defaults": [{"key": "Env", "allowed_values": ["Prod", "prod"]}]}, "duplicate allowed values"),
    ({"defaults": [{"key": "Env", "allowed_values": [" "]}]}, "must not be blank"),
    ({"defaults": [{"key": "Env", "aliases": {"p": "Prod"}}]}, "aliases require allowed_values"),
    ({"defaults": [{"key": "Env", "allowed_values": ["Prod"], "aliases": {"p": "Production"}}]}, "not an allowed value"),
    ({"defaults": [{"key": "Env", "allowed_values": ["Prod"], "aliases": {"PROD": "Prod"}}]}, "collides with an allowed value"),
    ({"defaults": [{"key": "Owner"}], "unexpected": 1}, "Extra inputs"),
    (with_overrides({"AWS::RDS::DBInstance": {"remove": ["Owner"]}}), "unsupported resource type"),
    (with_overrides({"AWS::S3::Bucket": {"remove": ["Nope"]}}), "aren't in defaults"),
    (with_overrides({"AWS::S3::Bucket": {"remove": ["Owner"], "rules": [{"key": "owner"}]}}), "both overridden and removed"),
    (with_overrides({"AWS::S3::Bucket": {"rules": [{"key": "A"}, {"key": "a"}]}}), "duplicate override rule keys"),
    (with_overrides({"AWS::S3::Bucket": {"remove": ["Environment", "Project", "Owner", "CostCenter"]}}), "no mandatory tags"),
])
def test_invalid_policies_are_rejected(data, fragment):
    assert fragment in errors_for(data)


def test_parse_accepts_json_text_and_reports_all_errors():
    with pytest.raises(PolicyValidationError) as exc:
        parse_policy('{"defaults": [{"key": "Owner", "required": "maybe"}]}')
    assert exc.value.errors and "required" in exc.value.errors[0]


# ---------- hashing ----------

def test_content_hash_is_stable_and_sensitive_to_changes():
    a = parse_policy(BASE)
    b = parse_policy(BASE)
    assert a.content_hash() == b.content_hash() and a.content_hash().startswith("sha256:")
    changed = parse_policy({**BASE, "defaults": [*BASE["defaults"], {"key": "Team"}]})
    assert changed.content_hash() != a.content_hash()


def test_documents_are_immutable():
    doc = PolicyDocument.model_validate(BASE)
    with pytest.raises(Exception):
        doc.defaults = []
