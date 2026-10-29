"""Minimal remote MCP server (Streamable HTTP transport, JSON responses) with x402 pay-per-call.

Transport: clients POST JSON-RPC 2.0 messages to /mcp; the server answers each request with a single
`application/json` response (allowed by the Streamable HTTP transport), notifications get 202. GET /mcp
(server-initiated SSE stream) is not offered and answers 405. The server is stateless: no session id.

Payments follow the x402 MCP transport (https://github.com/coinbase/x402/blob/main/specs/transports-v2/mcp.md):
a paid tool called without payment returns a tool result with `isError: true` whose `structuredContent`
(and JSON-encoded `content[0].text`) is the x402 v2 `PaymentRequired` object; the client retries with the
signed PaymentPayload in `params._meta["x402/payment"]`; after settlement the result carries
`_meta["x402/payment-response"]`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PAYMENT_META = "x402/payment"
PAYMENT_RESPONSE_META = "x402/payment-response"
MAX_BODY = 64 * 1024


@dataclass
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    handler: Callable[[dict, dict, Any], dict]  # (arguments, _meta, request) -> CallToolResult
    price: dict | None = None  # x402 price metadata shown in tools/list `_meta`
    annotations: dict = field(default_factory=lambda: {"readOnlyHint": True, "openWorldHint": True})

    def describe(self) -> dict:
        d = {"name": self.name, "title": self.title, "description": self.description,
             "inputSchema": self.input_schema, "annotations": {"title": self.title, **self.annotations}}
        if self.price:
            d["_meta"] = {"x402": self.price}
        return d


def text_result(obj: Any, meta: dict | None = None) -> dict:
    r = {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False, default=str)}],
         "structuredContent": obj if isinstance(obj, dict) else {"result": obj}, "isError": False}
    if meta:
        r["_meta"] = meta
    return r


def error_result(message: str, details: Any = None) -> dict:
    obj = {"error": message, **({"details": details} if details is not None else {})}
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False, default=str)}],
            "structuredContent": obj, "isError": True}


def payment_required_result(payment_required: dict) -> dict:
    """x402 MCP transport: isError + PaymentRequired in structuredContent and content[0].text."""
    return {"content": [{"type": "text", "text": json.dumps(payment_required, separators=(",", ":"))}],
            "structuredContent": payment_required, "isError": True}


def _err(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class McpServer:
    def __init__(self, name: str, title: str, version: str, instructions: str, tools: list[Tool]):
        self.info = {"name": name, "title": title, "version": version}
        self.instructions = instructions
        self.tools = {t.name: t for t in tools}

    def handle_body(self, raw: bytes, request: Any) -> tuple[int, dict | None]:
        """Returns (HTTP status, JSON body or None for 202)."""
        if len(raw) > MAX_BODY:
            return 413, _err(None, -32600, "Request too large")
        try:
            msg = json.loads(raw or b"null")
        except ValueError:
            return 400, _err(None, -32700, "Parse error")
        if isinstance(msg, list):
            return 400, _err(None, -32600, "JSON-RPC batching is not supported")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return 400, _err(None, -32600, "Invalid Request")
        if "method" not in msg:  # a response from the client (we never send requests): accept
            return 202, None
        if "id" not in msg:  # notification
            return 202, None
        return 200, self.dispatch(msg, request)

    def dispatch(self, msg: dict, request: Any) -> dict:
        id_, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if not isinstance(params, dict):
            return _err(id_, -32602, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": self.info, "instructions": self.instructions}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": id_, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": id_, "result": {"tools": [t.describe() for t in self.tools.values()]}}
        if method == "tools/call":
            tool = self.tools.get(params.get("name"))
            if not tool:
                return _err(id_, -32602, f"Unknown tool: {params.get('name')}")
            args = params.get("arguments") or {}
            meta = params.get("_meta") or {}
            if not isinstance(args, dict) or not isinstance(meta, dict):
                return _err(id_, -32602, "arguments and _meta must be objects")
            return {"jsonrpc": "2.0", "id": id_, "result": tool.handler(args, meta, request)}
        if method in ("resources/list", "prompts/list"):
            key = method.split("/")[0]
            return {"jsonrpc": "2.0", "id": id_, "result": {key: []}}
        return _err(id_, -32601, f"Method not found: {method}")
