"""Fixture-mode CloudGuard deployment server for the Playwright suite, with an AWS-call tripwire.

Started by playwright.config.ts (cwd: backend/). Token digests come from the environment the config sets; tokens
are random per run and never written to disk. Every botocore API call is appended to CG_E2E_AWS_LOG and then
refused, so the suite can prove fixture mode made zero AWS calls.
"""

import os
import sys
from pathlib import Path

log = Path(os.environ["CG_E2E_AWS_LOG"])
log.write_text("", encoding="utf-8")
for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
    os.environ.pop(key, None)
os.environ["CLOUDGUARD_MODE"] = "fixture"

try:
    import botocore.client

    def tripwire(self, operation, params):
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{self.meta.service_model.service_name}:{operation}\n")
        raise RuntimeError("AWS API call attempted in fixture mode")

    botocore.client.BaseClient._make_api_call = tripwire
except ImportError:  # boto3 not importable: no AWS call is possible at all
    pass

from cloudguard.deploy.__main__ import main  # noqa: E402

sys.exit(main(["--port", os.environ.get("CG_E2E_BACKEND_PORT", "8091")]))
