# Live tagging (Checkpoint 5)

CloudGuard can write tags to Lambda functions, DynamoDB tables, EC2 instances and S3 buckets in the authenticated
AWS account, but only by executing a proposal that a second person approved. Everything up to approval is
read-only, and nothing a voice or MCP client says can apply a tag.

**Status:** implemented and tested offline only (botocore Stubber and an in-memory fake account). **Not yet
verified against real AWS.** Do not rely on it until it has been run against a separately approved test resource.

## Workflow

| Step | Who | Where | Writes to AWS |
|---|---|---|---|
| Find resources by name | anyone with a token | MCP `search_resources` (also the simulator) | no |
| Preview the change | operator token, or the Alexa token (projected: no ARNs, account IDs or current values) | MCP `preview_tag_changes`; the simulator's tagging conversation runs it only after an explicit yes to the repeated summary | no (stores nothing) |
| Save the proposal | requester (STS-verified, allow-listed `create`) | `proposal save --request-json FILE --mode live` | no |
| Approve its exact content hash | a different person (`approve`) | `proposal approve ID --content-hash H [--acknowledge-overwrites] --mode live` | no |
| Execute | someone who is not the approver (`execute`) | `proposal execute ID --approval-id A --mode live [--tagger-profile P]` | **yes** |

```bash
python -m cloudguard.governance proposal save --request-json request.json --mode live --region ap-south-1
python -m cloudguard.governance proposal approve PROPOSAL_ID --content-hash sha256:... --mode live --profile approver
python -m cloudguard.governance proposal execute PROPOSAL_ID --approval-id APPROVAL_ID --mode live --tagger-profile tagger
```

For a personal single-operator development account there is also a voice path that runs these same steps for one person, without the CLI: see `docs/voice-tagging.md`. Approval and execution are deliberately not MCP tools: MCP bearer tokens identify a configured token, not a person,
so they can't support separation of duties. The CLI uses each caller's own AWS credentials, verified with
sts:GetCallerIdentity and mapped to people by the allow-list (docs/authorization.md).

## Per-service APIs

| Type | Read (revalidate, pre-write, verify) | Write |
|---|---|---|
| Lambda function | `lambda:ListTags` | `lambda:TagResource` (tag map) |
| DynamoDB table | `dynamodb:DescribeTable` (TableId) + `ListTagsOfResource` (paginated) | `dynamodb:TagResource` (Key/Value list) |
| EC2 instance | `ec2:DescribeInstances` | `ec2:CreateTags` on the instance ID |
| S3 bucket | `s3:GetBucketLocation`, `ListBuckets` (CreationDate), `GetBucketTagging` | `s3:PutBucketTagging` with the merged set |

Code: `backend/cloudguard/governance/remediation/live_writer.py`. The writer adds or overwrites only the approved
keys and never removes a tag. Least-privilege role: `infra/iam/proposed/tagger-role-policy.json` (a design, not
deployed; the inspector role explicitly denies every tagging API).

## Safety properties (all enforced in code and tested offline)

- No AWS write before an APPROVED proposal; preview, save and identity capture only read.
- Approval binds the immutable proposal: its content hash and the resources' immutable identities (TableId,
  instance ID, bucket CreationDate). A recreated resource is skipped, not tagged.
- Approvals expire (15 minutes by default, never beyond the proposal); an expired or used approval never executes;
  a new proposal needs its own approval; the requester can't approve; the approver can't execute.
- Adds never overwrite: an existing different value is a conflict. Updates are explicit (one tag) and their
  approval must pass `--acknowledge-overwrites`.
- `aws:` and `cloudguard:` keys are refused server-side; keys and values are validated against AWS limits.
- Every resource is re-read immediately before its write (fenced by a lease and a recorded write intent) and read
  back afterwards; each resource gets its own result (APPLIED_VERIFIED, ALREADY_APPLIED, SKIPPED, FAILED_WRITE,
  FAILED_VERIFICATION). Throttling is retried within a bounded write budget; access denied is not.
- AWS error messages are not recorded (they can name the caller); only the error code and operation are.

## Known limits

- **S3 is read-merge-write.** PutBucketTagging replaces the whole tag set, so a tag another actor adds between the
  pre-write read and the write is lost. Verification reports unrelated tags that changed, but can't prevent it.
- No tagging API accepts a fencing token: a write delayed past its deadline can still land late (see executor.py).
- Lambda has no immutable identity: a function deleted and recreated with the same name between approval and
  execution is not detected.
- Only EC2 instances are supported among EC2 resources (no volumes, snapshots or security groups).
- Not verified against real AWS, the real IAM role, or real eventual consistency.
