# Friction Log

Submitted with the Devpost entry (worth up to a 10% judging bonus). Log each snag with Amazon/AWS tools when it happens: Alexa+ MCP docs, Bedrock, Strands, Kiro Crew, CloudFormation, IAM and so on.

Fields: task · steps taken · expected vs actual · severity (Critical / High / Medium / Low) · workaround · actionable suggestion.

---

## FL-001 — S3 signals "no tags" as an error (`NoSuchTagSet`)
- **Task:** Tell an untagged bucket apart from a bucket whose tags we're not allowed to read.
- **Steps:** `s3.get_bucket_tagging(Bucket=...)` on a bucket that has never been tagged.
- **Expected:** `TagSet: []`, as Lambda `ListTags` and DynamoDB `ListTagsOfResource` return for untagged resources.
- **Actual:** A `ClientError` with code `NoSuchTagSet` (HTTP 404). Every tool must special-case this error code, or it risks reporting "inspection failed" for ordinary untagged buckets, or worse, treating real failures as "untagged".
- **Severity:** Medium (it silently skews governance results if handled naively)
- **Workaround:** Map `NoSuchTagSet` to an empty tag set; every other error code becomes an inspection failure.
- **Suggestion:** Return an empty `TagSet` with HTTP 200, or document the special case in the boto3 `get_bucket_tagging` docstring.

<!-- FL-002 — <title>
- **Task:**
- **Steps:**
- **Expected:**
- **Actual:**
- **Severity:**
- **Workaround:**
- **Suggestion:**
-->
