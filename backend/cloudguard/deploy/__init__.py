"""Deployment prototype: the existing MCP server as an authenticated HTTP app for AWS Lambda Web Adapter.

PROTOTYPE. Not deployed. Fixture or live mode; live mode makes no AWS call before an authenticated, authorized
tool call. It wraps cloudguard.server.app.create_server (all twelve tools); it defines no tools of its own. The existing entry points (`python -m cloudguard.server`, stdio and local HTTP) are unchanged.
"""
