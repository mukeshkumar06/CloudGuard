#!/bin/sh
# PROTOTYPE (not deployed). Lambda handler script for AWS Lambda Web Adapter, zip packaging:
#   runtime python3.14, handler run.sh,
#   layer arn:aws:lambda:<region>:753240598075:layer:LambdaAdapterLayerX86:30 (adapter 1.1.0; Arm64 likewise),
#   AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap, AWS_LWA_READINESS_CHECK_PATH=/healthz, PORT=8080,
#   AWS_LWA_INVOKE_MODE=buffered, CLOUDGUARD_MODE=fixture|live, CLOUDGUARD_TOKEN_SHA256=<hex>,
#   CLOUDGUARD_ALLOWED_HOSTS=<function-url-host>; function timeout of at least 60 s (see README)
# The adapter waits for GET /healthz on 127.0.0.1:$PORT, then forwards each Function URL request to it.
export PYTHONPATH="${LAMBDA_TASK_ROOT}:${PYTHONPATH}"
exec python -m cloudguard.deploy --port "${PORT:-8080}"
