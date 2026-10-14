"""Natural-language tagging requests -> structured TaggingRequest.

RuleBasedParser handles a small, documented grammar deterministically. It returns:
- a TaggingRequest when the text matches a supported shape,
- a Rejection when the text is recognisably unsafe or unsupported (e.g. tag removal, a plural type
  without "all"); a fallback parser must not override these,
- None when it doesn't recognise the text at all, leaving room for a fallback parser.

CompositeParser chains parsers. A model-based (Bedrock) parser can be appended in a later phase;
whatever any parser returns is still validated server-side by the engine before anything else.

Supported shapes (case-insensitive keywords; names, keys and values keep their case):
    tag <target> with K=V[, K=V ...]
    add K=V[, K=V ...] to <target>
    update|change|set [the] KEY [tag] of|on|for <target> to VALUE
<target> is   [the] [<type>] NAME|ARN [in [region] REGION] [in account 123456789012]
          or  all <types> [in [region] REGION] [in account 123456789012]
<type>   is   lambda function | dynamodb table | s3 bucket | ec2 instance (singular or plural)
Values containing commas, " and ", " to " or " with " must be double-quoted.
"""

from __future__ import annotations

import re
from typing import Protocol

from cloudguard.governance.custom_tagging.models import (
    Rejection,
    RejectionCode,
    TaggingOperation,
    TaggingRequest,
    TargetCriteria,
)

_TYPES: list[tuple[str, str, str]] = [
    # (singular regex, plural regex, resource type)
    (r"lambda\s+function|lambda", r"lambda\s+functions|lambdas", "AWS::Lambda::Function"),
    (r"dynamodb\s+table|dynamo\s+table", r"dynamodb\s+tables|dynamo\s+tables", "AWS::DynamoDB::Table"),
    (r"s3\s+bucket|bucket", r"s3\s+buckets|buckets", "AWS::S3::Bucket"),
    (r"ec2\s+instance", r"ec2\s+instances", "AWS::EC2::Instance"),
]
# Bounds parsing work for every caller (the CLI now, an MCP endpoint later). Fifty max-length tags
# would exceed this, but no single tagging request needs to be that large.
MAX_REQUEST_CHARS = 4000
_REGION = r"[a-z]{2}(?:-gov)?-[a-z]+-\d"
_QUAL = re.compile(
    rf"\s+(?:in\s+(?:the\s+)?(?:region\s+)?(?P<region>{_REGION})|(?:in\s+|of\s+)?account\s+(?P<account>\d{{12}}))",
    re.IGNORECASE,
)
_UNSUPPORTED_VERBS = re.compile(r"^(remove|delete|untag|clear|strip|drop|rename|copy)\b", re.IGNORECASE)

_TAG_FORM = re.compile(r"^(?:tag|label)\s+(?P<target>.+?)\s+with\s+(?:tags?\s+)?(?P<tags>.+)$", re.IGNORECASE)
_ADD_FORM = re.compile(r"^add\s+(?:the\s+)?(?:tags?\s+)?(?P<tags>.+?)\s+to\s+(?P<target>.+)$", re.IGNORECASE)
_UPDATE_FORM = re.compile(
    r'^(?:update|change|set)\s+(?:the\s+)?(?P<key>"[^"]+"|[^\s="]+)(?:\s+tag)?\s+(?:of|on|for)\s+'
    r"(?P<target>.+?)\s+to\s+(?P<value>.+)$",
    re.IGNORECASE,
)


class RequestParser(Protocol):
    name: str

    def parse(self, text: str) -> TaggingRequest | Rejection | None: ...


def _reject(code: RejectionCode, message: str, hint: str | None = None) -> Rejection:
    return Rejection(code=code, message=message, hint=hint)


def _unquote(s: str) -> str:
    s = s.strip()
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] == '"' else s


def _split_outside_quotes(s: str) -> list[str]:
    parts, buf, quoted, i = [], [], False, 0
    while i < len(s):
        c = s[i]
        if c == '"':
            quoted = not quoted
        if not quoted and c == ",":
            parts.append("".join(buf))
            buf, i = [], i + 1
            continue
        if not quoted and s[i:i + 5].lower() == " and ":
            parts.append("".join(buf))
            buf, i = [], i + 5
            continue
        buf.append(c)
        i += 1
    if quoted:
        raise ValueError("unbalanced double quotes")
    parts.append("".join(buf))
    return parts


def parse_tag_list(text: str) -> dict[str, str] | Rejection:
    try:
        parts = _split_outside_quotes(text)
    except ValueError as exc:
        return _reject(RejectionCode.MALFORMED_TAGS, str(exc))
    tags: dict[str, str] = {}
    for part in parts:
        part = part.strip()
        m = re.match(r'^(?P<k>"[^"]*"|[^="]+?)\s*=\s*(?P<v>"[^"]*"|[^"]*)$', part)
        if not m:
            return _reject(RejectionCode.MALFORMED_TAGS, f"expected KEY=VALUE, got {part!r}",
                           'quote values that contain commas or the word "and", e.g. Owner="Platform and Data"')
        key, value = _unquote(m["k"]), _unquote(m["v"])
        if key.lower() in {k.lower() for k in tags}:
            return _reject(RejectionCode.MALFORMED_TAGS, f"tag key {key!r} appears more than once (keys compare case-insensitively)")
        tags[key] = value
    return tags


def _match_type(text: str, plural: bool) -> tuple[str, str] | None:
    """(resource_type, remaining text) if text starts with a type phrase."""
    for singular, plural_re, rtype in _TYPES:
        pattern = plural_re if plural else singular
        m = re.match(rf"^(?:{pattern})\b", text, re.IGNORECASE)
        if m:
            return rtype, text[m.end():]
    return None


def _parse_qualifiers(text: str) -> tuple[str | None, str | None] | Rejection:
    region = account = None
    pos = 0
    while pos < len(text):
        m = _QUAL.match(text, pos)
        if not m:
            return _reject(RejectionCode.UNSUPPORTED_REQUEST, f"couldn't understand {text[pos:].strip()!r} in the target",
                           "qualifiers supported: 'in <region>' and 'in account <12-digit id>'")
        if m["region"]:
            if region:
                return _reject(RejectionCode.AMBIGUOUS_REQUEST, "more than one region given")
            region = m["region"].lower()
        if m["account"]:
            if account:
                return _reject(RejectionCode.AMBIGUOUS_REQUEST, "more than one account given")
            account = m["account"]
        pos = m.end()
    return region, account


def parse_target(text: str, bulk_expected: bool | None = None) -> TargetCriteria | Rejection:
    text = re.sub(r"^the\s+", "", text.strip(), flags=re.IGNORECASE)
    m_all = re.match(r"^all\s+(?:of\s+)?(?:the\s+|my\s+)?(?P<rest>.+)$", text, re.IGNORECASE)
    if m_all:
        rest = m_all["rest"]
        typed = _match_type(rest, plural=True) or _match_type(rest, plural=False)
        if typed is None:
            return _reject(RejectionCode.UNSUPPORTED_REQUEST, f"bulk tagging needs an explicit supported resource type, got {rest!r}",
                           "e.g. 'all Lambda functions', 'all DynamoDB tables', 'all S3 buckets', 'all EC2 instances'")
        rtype, tail = typed
        quals = _parse_qualifiers(tail)
        if isinstance(quals, Rejection):
            return quals
        return TargetCriteria(resource_type=rtype, region=quals[0], account_id=quals[1], all_of_type=True)

    if plural := _match_type(text, plural=True):
        phrase = text[: len(text) - len(plural[1])]
        return _reject(RejectionCode.AMBIGUOUS_REQUEST,
                       f"'{phrase}' refers to several resources, but the request doesn't say 'all'",
                       f"say 'all {phrase}' to tag every one of them, or name a single resource")

    rtype = None
    typed = _match_type(text, plural=False)
    if typed:
        rtype, text = typed
        text = text.lstrip()
    m = re.match(r'^(?P<ident>"[^"]+"|\S+)(?P<tail>.*)$', text)
    if not m or not text:
        return _reject(RejectionCode.AMBIGUOUS_REQUEST, "no resource name or ARN given",
                       "name one resource, or say 'all <type>'")
    ident = _unquote(m["ident"])
    if _match_type(ident, plural=False) or _match_type(ident, plural=True) or ident.lower() in ("all", "every", "each"):
        return _reject(RejectionCode.AMBIGUOUS_REQUEST, "no resource name or ARN given",
                       "name one resource, or say 'all <type>'")
    quals = _parse_qualifiers(m["tail"])
    if isinstance(quals, Rejection):
        return quals
    if ident.lower().startswith("arn:"):
        return TargetCriteria(resource_type=rtype, arn=ident, region=quals[0], account_id=quals[1])
    return TargetCriteria(resource_type=rtype, name=ident, region=quals[0], account_id=quals[1])


class RuleBasedParser:
    name = "rules"

    def parse(self, text: str) -> TaggingRequest | Rejection | None:
        raw = text
        if len(text) > MAX_REQUEST_CHARS:
            return _reject(RejectionCode.UNSUPPORTED_REQUEST, f"request exceeds {MAX_REQUEST_CHARS} characters",
                           "split it into smaller requests")
        text = re.sub(r"\s+", " ", text.strip()).rstrip(".!")
        if not text:
            return _reject(RejectionCode.UNSUPPORTED_REQUEST, "empty request")
        if _UNSUPPORTED_VERBS.match(text):
            return _reject(RejectionCode.UNSUPPORTED_REQUEST, f"'{text.split()[0]}' is not a supported tagging operation",
                           "supported: add or update tags (ADD_TAG, UPDATE_TAG, BULK_ADD_TAG, BULK_UPDATE_TAG)")

        if m := _UPDATE_FORM.match(text):
            target = parse_target(m["target"])
            if isinstance(target, Rejection):
                return target
            key, value = _unquote(m["key"]), _unquote(m["value"])
            if '"' in value or ("," in m["value"] and not m["value"].strip().startswith('"')):
                return _reject(RejectionCode.MALFORMED_TAGS, f"couldn't read a single value from {m['value']!r}",
                               "update one tag at a time; quote values with commas")
            op = TaggingOperation.BULK_UPDATE_TAG if target.all_of_type else TaggingOperation.UPDATE_TAG
            return TaggingRequest(operation=op, target=target, tags={key: value}, raw_text=raw, parsed_by=self.name)

        for form in (_TAG_FORM, _ADD_FORM):
            if m := form.match(text):
                target = parse_target(m["target"])
                if isinstance(target, Rejection):
                    return target
                tags = parse_tag_list(m["tags"])
                if isinstance(tags, Rejection):
                    return tags
                op = TaggingOperation.BULK_ADD_TAG if target.all_of_type else TaggingOperation.ADD_TAG
                return TaggingRequest(operation=op, target=target, tags=tags, raw_text=raw, parsed_by=self.name)
        return None


class CompositeParser:
    """First parser that recognises the text wins. Rule rejections are final."""

    name = "composite"

    def __init__(self, *parsers: RequestParser):
        self.parsers = parsers

    def parse(self, text: str) -> TaggingRequest | Rejection:
        for parser in self.parsers:
            result = parser.parse(text)
            if result is not None:
                return result
        return _reject(
            RejectionCode.UNSUPPORTED_REQUEST,
            "request not understood",
            "try: 'Tag Lambda function NAME with Key=Value', 'Add Key=Value to all DynamoDB tables', "
            "or 'Update Key of NAME to Value'",
        )
