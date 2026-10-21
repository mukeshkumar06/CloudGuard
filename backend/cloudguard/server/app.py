"""Builds the CloudGuard FastMCP server. The single place tools are registered."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from cloudguard.server.context import CostContext
from cloudguard.server.cost_tools import register_cost_tools

INSTRUCTIONS = (
    "CloudGuard AI answers questions about this AWS account's spending, read-only. Figures are AWS Cost "
    "Explorer UnblendedCost for UTC billing months; the calculated net is a CloudGuard calculation, not the "
    "AWS invoice. The current month is month to date, never a final bill. Attribution names the services and "
    "charge types that changed, never a cause. Use each result's `speech` for a short spoken reply and the "
    "`report` for evidence. An outcome of 'unavailable' means no figures, not zero spend."
)

# HTTP front ends that authenticate callers put the caller here in the ASGI scope (see cloudguard.deploy).
PRINCIPAL_SCOPE_KEY = "cloudguard.principal"

# (principal or None, tool name) -> allowed. None principal means the request carried no authenticated caller.
ToolAuthorizer = Callable[[Any, str], bool]


class CloudGuardMCP(FastMCP):
    """FastMCP that rejects unknown tool arguments instead of silently ignoring them.

    FastMCP's argument models ignore extra keys, so an argument such as `account_id` or `role_arn` would be
    dropped without notice. Rejecting it makes the account-context rule visible to clients.

    With an `authorizer`, every tool call is first checked against the authenticated caller that the HTTP
    front end stored in the request scope, before any argument handling or tool code (so before any AWS
    call). A call with no authenticated caller is refused. Without one (stdio, local HTTP) nothing changes.
    """

    authorizer: ToolAuthorizer | None = None

    def _principal(self) -> Any:
        try:
            request = self.get_context().request_context.request
        except (LookupError, ValueError):
            return None
        scope = getattr(request, "scope", None)
        return scope.get(PRINCIPAL_SCOPE_KEY) if isinstance(scope, dict) else None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.authorizer is not None and not self.authorizer(self._principal(), name):
            raise ToolError(f"not authorized to call {name[:64]}")
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            unknown = sorted(set(arguments or {}) - set(tool.parameters.get("properties", {})))
            if unknown:
                shown = ", ".join(repr(u[:40]) for u in unknown[:5])
                raise ToolError(f"{name} does not accept argument(s): {shown}")
        return await super().call_tool(name, arguments)


def create_server(cost: CostContext, *, port: int = 8000, authorizer: ToolAuthorizer | None = None) -> CloudGuardMCP:
    server = CloudGuardMCP(name="cloudguard-ai", instructions=INSTRUCTIONS, host="127.0.0.1", port=port,
                           stateless_http=True, json_response=True)
    server.authorizer = authorizer
    register_cost_tools(server, cost)
    for tool in server._tool_manager.list_tools():
        tool.parameters["additionalProperties"] = False  # advertise the strictness enforced in call_tool
    return server
