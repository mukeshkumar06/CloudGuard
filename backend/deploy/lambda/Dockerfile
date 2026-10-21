# PROTOTYPE (not deployed): CloudGuard MCP server on the AWS Lambda Python 3.14 base image with Lambda Web Adapter.
# See deploy/lambda/README.md for configuration, the authentication boundary, timeouts and open blockers.
# Build from backend/:  docker build -f deploy/lambda/Dockerfile -t cloudguard-mcp-lambda-proto .
#
# Fixture mode by default; CLOUDGUARD_MODE=live uses the execution role's credentials (read-only tools).
# No AWS credentials or secrets are baked in: the bearer-token digest (CLOUDGUARD_TOKEN_SHA256) and the
# allowed Host list (CLOUDGUARD_ALLOWED_HOSTS) are supplied at run time.
# The image's own entrypoint starts the web app, as the adapter's docs require for AWS managed base images.
# Local emulator test: docker run --entrypoint /usr/local/bin/aws-lambda-rie ... python -m cloudguard.deploy

FROM public.ecr.aws/lambda/python:3.14

# Lambda Web Adapter pinned to release 1.1.0 by manifest-list digest (resolved from public.ecr.aws on 2026-10-20).
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0@sha256:17cfd08eff1dfea3f6a9a1e9c65fdac80aa4919b6085e746615530f43f57d2f1 /lambda-adapter /opt/extensions/lambda-adapter

COPY deploy/lambda/requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir --target "${LAMBDA_TASK_ROOT}" -r "${LAMBDA_TASK_ROOT}/requirements.txt"

# Only the application package and the synthetic fixtures; no tests, no .venv, no local policy store.
COPY cloudguard ${LAMBDA_TASK_ROOT}/cloudguard
COPY fixtures ${LAMBDA_TASK_ROOT}/fixtures

ENV PORT=8080 \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/healthz \
    AWS_LWA_INVOKE_MODE=buffered \
    CLOUDGUARD_MODE=fixture \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/var/task

WORKDIR /var/task
ENTRYPOINT ["python", "-m", "cloudguard.deploy"]
