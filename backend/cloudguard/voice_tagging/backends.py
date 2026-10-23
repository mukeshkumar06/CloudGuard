"""Where voice tagging gets identity, discovery and writes. Both reuse the existing implementations:

- LiveBackend: the server's own AWS credentials, verified with sts:GetCallerIdentity (StsCallerIdentityProvider);
  discovery with LiveSource, narrowed to one region and service; writes with LiveTagAccount on a session for the
  configured tagging role (sts:AssumeRole, 15 minutes), whose account is checked again before use.
- FixtureBackend: the simulated FixtureAccount (no AWS at all) with a development identity. Its fixture allow-list
  is seeded on first use, in the voice tagging store only, and can never authorise a live operation.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from cloudguard.governance.discovery import DiscoveryResult, LiveSource
from cloudguard.governance.discovery.common import SINGLE_CALL_CONFIG
from cloudguard.governance.remediation.authz import AuthzManager, parse_authz
from cloudguard.governance.remediation.identity import DevelopmentIdentityProvider, StsCallerIdentityProvider
from cloudguard.governance.remediation.models import OperatingMode
from cloudguard.governance.remediation.writers import FixtureAccount

FIXTURE_OPERATOR = "voice-operator"
FIXTURE_ADMIN = "voice-admin"  # the allow-list's admin must be a different person from its approvers
ROLE_SESSION_SECONDS = 900


class _Scoped:
    """A discovery source answering for one region and one service only."""

    mode = "live"

    def __init__(self, source: LiveSource, region: str, service: str):
        self._source, self._region, self._service = source, region, service

    def discover(self) -> DiscoveryResult:
        return self._source.discover(regions=[self._region], services=[self._service])


class LiveBackend:
    mode = OperatingMode.LIVE
    label = "live"

    def __init__(self, session, role_arn: str, *, sts_client=None,
                 source_factory: Callable[[str, str], object] | None = None,
                 tag_account_factory: Callable[[str], object] | None = None):
        self.session, self.role_arn = session, role_arn
        self._sts = sts_client
        self._source_factory = source_factory
        self._tag_account_factory = tag_account_factory  # tests: a LiveTagAccount over a fake AWS

    def _sts_client(self):
        if self._sts is None:
            self._sts = self.session.client("sts", config=SINGLE_CALL_CONFIG)
        return self._sts

    def identity(self):
        return StsCallerIdentityProvider(self._sts_client())

    def source(self, region: str, service: str):
        if self._source_factory is not None:
            return self._source_factory(region, service)
        return _Scoped(LiveSource(self.session, [region]), region, service)

    def tag_account(self, account_id: str):
        if self._tag_account_factory is not None:
            return self._tag_account_factory(account_id)
        import boto3

        from cloudguard.governance.remediation.live_writer import LiveTagAccount
        # The session name fits the proposed tagger trust policy (infra/iam/proposed/tagger-trust-policy.json), which
        # only lets the CloudGuard backend role assume it with a "cg-proposal-*" session.
        creds = self._sts_client().assume_role(RoleArn=self.role_arn,
                                               RoleSessionName=f"cg-proposal-voice-{secrets.token_hex(6)}",
                                               DurationSeconds=ROLE_SESSION_SECONDS)["Credentials"]
        writer = boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                               aws_session_token=creds["SessionToken"], region_name=self.session.region_name)
        actual = writer.client("sts", config=SINGLE_CALL_CONFIG).get_caller_identity()["Account"]
        return LiveTagAccount(writer, actual)  # the service refuses it unless `actual` is the verified account

    def execution_identity(self) -> str:
        return self.role_arn


class FixtureBackend:
    mode = OperatingMode.FIXTURE
    label = "fixture"

    def __init__(self, account: FixtureAccount):
        self.account = account
        self.account_id = account.account_id

    def identity(self):
        return DevelopmentIdentityProvider(FIXTURE_OPERATOR)

    def source(self, region: str, service: str):
        return self.account  # FixtureAccount.discover(): the simulated account's current state

    def tag_account(self, account_id: str):
        return self.account

    def execution_identity(self) -> str:
        return f"dev:{FIXTURE_OPERATOR} (simulated account)"


def seed_fixture_authz(authz: AuthzManager, account_id: str) -> None:
    """Fixture mode only: let the development operator create, approve and execute in the simulated account."""
    if authz.store.active_version(authz.config_id) is not None:
        return
    who, admin = f"dev:{FIXTURE_OPERATOR}", f"dev:{FIXTURE_ADMIN}"
    grants = {p: [who] for p in ("create", "approve", "reject", "execute")} | {"admin": [admin]}
    config = parse_authz({"accounts": {account_id: grants}})
    seeding = AuthzManager(authz.store, bootstrap_admin=[admin])
    seeding.save(config, expected_version=None, identity=DevelopmentIdentityProvider(FIXTURE_ADMIN),
                 note="voice tagging fixture operator")
