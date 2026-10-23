"""Shared helpers for service-specific discovery: error classification and boto3 client config."""

from __future__ import annotations

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from cloudguard.governance.models import ErrorKind, InspectionError

# Standard retry mode backs off on throttling and transient 5xx before we give up on a call. Explicit timeouts
# (botocore's defaults are 60 s each) keep one slow endpoint from holding a discovery task past its deadline.
BOTO_CONFIG = Config(retries={"mode": "standard", "max_attempts": 5}, connect_timeout=5, read_timeout=15,
                     user_agent_extra="cloudguard-ai")

# Single calls that run before discovery's tasks start (sts:GetCallerIdentity, ec2:DescribeRegions on a live
# server's first scan). botocore's max_attempts counts retries after the first try, so this is 2 attempts: worst
# case about 2 x (3 + 5) s plus a short backoff. With DISCOVERY_DEADLINE_SECONDS, a first live scan stays under the
# simulator's 60-second call timeout.
SINGLE_CALL_CONFIG = Config(retries={"mode": "standard", "max_attempts": 1}, connect_timeout=3, read_timeout=5,
                            user_agent_extra="cloudguard-ai")

AWS_ERRORS = (ClientError, BotoCoreError)

_ACCESS_DENIED = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AuthorizationError",
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "ExpiredToken",
    "ExpiredTokenException",
}
_THROTTLED = {
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "SlowDown",
    "ProvisionedThroughputExceededException",
}
_NOT_FOUND = {"ResourceNotFoundException", "NoSuchBucket", "InvalidInstanceID.NotFound"}
_UNAVAILABLE = {"ServiceUnavailable", "ServiceUnavailableException", "InternalError", "InternalFailure", "InternalServerError"}


def classify_error(exc: Exception, operation: str) -> InspectionError:
    """Map a boto3 exception to an InspectionError. Messages are AWS's own text, never credentials."""
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        code = str(err.get("Code", "Unknown"))
        message = str(err.get("Message", ""))[:500]
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code in _ACCESS_DENIED:
            kind = ErrorKind.ACCESS_DENIED
        elif code in _THROTTLED:
            kind = ErrorKind.THROTTLED
        elif code in _NOT_FOUND:
            kind = ErrorKind.NOT_FOUND
        elif code in _UNAVAILABLE or status >= 500:
            kind = ErrorKind.SERVICE_UNAVAILABLE
        else:
            kind = ErrorKind.UNKNOWN
        return InspectionError(kind=kind, code=code, message=message, operation=operation)
    if isinstance(exc, EndpointConnectionError):
        return InspectionError(
            kind=ErrorKind.SERVICE_UNAVAILABLE, code=type(exc).__name__, message=str(exc)[:500], operation=operation
        )
    return InspectionError(kind=ErrorKind.UNKNOWN, code=type(exc).__name__, message=str(exc)[:500], operation=operation)


def error_code(exc: Exception) -> str | None:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code")
    return None
