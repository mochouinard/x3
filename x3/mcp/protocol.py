"""MCP over stdio: one JSON-RPC 2.0 message per line, in and out.

Only what a tools-only server needs: ``initialize``, ``ping``,
``tools/list`` and ``tools/call``. Notifications (no ``id``) get no answer.

stdout is the wire. Anything else that prints -- a library's warning, a stray
debug line -- would corrupt it, so :func:`serve` points ``sys.stdout`` at
stderr for the duration and writes replies to the real one only.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable, TextIO

#: Newest first. A client asking for one of these gets it back; anything else
#: gets the newest, and the client decides whether it can live with that.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_NAME = "x3"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """A tool failed in a way the model should read and act on.

    Reported as a tool result with ``isError``, not a JSON-RPC error: the spec
    keeps protocol errors for protocol faults, and a wrong menu name is
    something the model can fix on its next call.
    """


class Server:
    """Dispatch for one MCP session.

    ``tools`` is a list of (definition, handler). The definition is the
    ``tools/list`` entry; the handler takes the call's arguments and returns
    the text of the result.
    """

    def __init__(
        self,
        tools: list[tuple[dict[str, Any], Callable[[dict[str, Any]], str]]],
        *,
        version: str = "",
        instructions: str = "",
    ) -> None:
        self._defs = [d for d, _h in tools]
        self._handlers = {d["name"]: h for d, h in tools}
        self.version = version
        self.instructions = instructions

    def handle(self, msg: Any) -> dict[str, Any] | None:
        """The reply to one decoded message, or None when none is due."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = msg.get("method")
        mid = msg.get("id")
        if "id" not in msg:
            return None  # notification: initialized, cancelled, ...
        if not isinstance(method, str):
            # A response to something we never send, or junk.
            return None if "result" in msg or "error" in msg else _error(
                mid, INVALID_REQUEST, "no method"
            )
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _error(mid, INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            return _result(mid, self._initialize(params))
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": self._defs})
        if method == "tools/call":
            return self._call(mid, params)
        return _error(mid, METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        out: dict[str, Any] = {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": self.version or "0"},
        }
        if self.instructions:
            out["instructions"] = self.instructions
        return out

    def _call(self, mid: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        handler = self._handlers.get(name) if isinstance(name, str) else None
        if handler is None:
            return _error(mid, INVALID_PARAMS, f"unknown tool {name!r}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(mid, INVALID_PARAMS, "arguments must be an object")
        try:
            text, is_error = handler(args), False
        except ToolError as e:
            text, is_error = str(e), True
        except Exception as e:  # noqa: BLE001 -- the session must outlive a tool
            traceback.print_exc(file=sys.stderr)
            text, is_error = f"{type(e).__name__}: {e}", True
        return _result(mid, {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        })


def _result(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(server: Server, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Answer messages from ``stdin`` until it closes."""
    wire_in = stdin or sys.stdin
    wire_out = stdout or sys.stdout
    saved, sys.stdout = sys.stdout, sys.stderr
    try:
        for line in wire_in:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                reply: dict[str, Any] | None = _error(None, PARSE_ERROR, str(e))
            else:
                reply = server.handle(msg)
            if reply is not None:
                wire_out.write(json.dumps(reply, ensure_ascii=False) + "\n")
                wire_out.flush()
    finally:
        sys.stdout = saved
