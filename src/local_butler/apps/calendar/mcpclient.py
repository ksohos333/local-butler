"""Minimal MCP client for the local ``bigus_dickus`` server.

The server speaks Streamable HTTP: a JSON-RPC POST that answers with either
plain JSON or an SSE frame, and requires the session id handed out by
``initialize`` to be echoed on every later call. Only what the calendar needs is
implemented here -- handshake, tool listing, and tool invocation -- because the
point of this module is to let the calendar ask a question of the assistant's
own data, not to be a general MCP client.

Only an allow-list of read-only tools is exposed to the model. The server also
offers ``send_email``, ``import_transactions``, ``shell`` and ``python``; those
are never surfaced, because a chat box on a calendar should not be able to send
mail or run commands.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_MCP_URL = "http://127.0.0.1:8780/mcp"
PROTOCOL_VERSION = "2024-11-05"

# Tools the assistant may call. Read-only by design: the assistant reads mail to
# understand commitments, and proposes calendar writes for the user to approve.
ALLOWED_TOOLS = ("list_emails", "get_email", "search_emails")

# Present on the server, deliberately never offered to the model.
FORBIDDEN_TOOLS = (
    "send_email",
    "import_transactions",
    "shell",
    "python",
    "web_search",
)


class MCPError(RuntimeError):
    """Raised when the MCP server cannot be reached or answers badly."""


def _unwrap(body: str) -> Any:
    """Pull the JSON payload out of a Streamable HTTP response.

    The server may answer with a bare JSON body or a single SSE ``data:``
    frame; both are accepted so the client survives either mode.
    """
    text = (body or "").strip()
    if not text:
        return {}
    if text[0] in "{[":
        try:
            return json.loads(text)
        except ValueError as exc:
            raise MCPError("MCP server returned invalid JSON") from exc
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            try:
                return json.loads(payload)
            except ValueError as exc:
                raise MCPError("MCP server returned invalid JSON in SSE frame") from exc
    raise MCPError("MCP server returned an unrecognised response")


class MCPClient:
    """A lazily-handshaked client for one MCP server.

    The session is established on first use and reused afterwards. A lock keeps
    the GTK worker threads from racing to create two sessions, and a failed
    handshake is not cached so the next call can retry.
    """

    def __init__(self, url: str = DEFAULT_MCP_URL, timeout: float = 60.0) -> None:
        self.url = url
        self.timeout = timeout
        self._session: Optional[str] = None
        self._server_info: Dict[str, Any] = {}
        self._tools: Optional[List[Dict[str, Any]]] = None
        # Reentrant: tools() holds the lock while calling connect(), which
        # takes it again. A plain Lock would deadlock on the second acquire.
        self._lock = threading.RLock()

    # -- plumbing ---------------------------------------------------------

    def _post(
        self,
        method: str,
        params: Optional[Dict[str, Any]],
        request_id: Optional[int],
    ) -> Any:
        body_msg: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        if request_id is not None:
            body_msg["id"] = request_id
        payload = json.dumps(body_msg).encode("utf-8")
        request = urllib.request.Request(self.url, data=payload, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json, text/event-stream")
        if self._session:
            request.add_header("Mcp-Session-Id", self._session)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self._session = session
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise MCPError("MCP server returned {0}: {1}".format(exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise MCPError("cannot reach {0}: {1}".format(self.url, exc.reason)) from exc
        response = _unwrap(body)
        if isinstance(response, dict) and response.get("error"):
            detail = response["error"]
            # A notification is fire-and-forget: this server answers
            # notifications/initialized with "Method not found" because it is
            # a one-way message, and that is not a failure of the handshake.
            if request_id is None:
                return {}
            raise MCPError(str(detail.get("message", "MCP error")))
        if not isinstance(response, dict):
            return {}
        return response.get("result", {})

    def connect(self) -> None:
        """Run the initialize handshake if it has not run yet."""
        with self._lock:
            if self._session:
                return
            previous = self._session
            try:
                result = self._post(
                    "initialize",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "local-butler-calendar", "version": "0.2"},
                    },
                    1,
                )
                self._server_info = result.get("serverInfo", {}) or {}
                self._post("notifications/initialized", {}, None)
            except Exception:
                self._session = previous
                raise

    def available(self) -> bool:
        """True when the server answers a tools listing."""
        try:
            self.tools()
            return True
        except MCPError:
            return False

    # -- tools ------------------------------------------------------------

    def tools(self) -> List[Dict[str, Any]]:
        """Every tool the server offers, cached after the first listing."""
        with self._lock:
            if self._tools is None:
                self.connect()
        if self._tools is None:
            result = self._post("tools/list", {}, 3)
            self._tools = list(result.get("tools", []) or [])
        return list(self._tools)

    def allowed_tools(self) -> List[Dict[str, Any]]:
        """The subset of tools the assistant is permitted to call."""
        return [tool for tool in self.tools() if tool.get("name") in ALLOWED_TOOLS]

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Invoke one allow-listed tool and return its decoded result.

        Rejects a name outside :data:`ALLOWED_TOOLS` before any request is made,
        so a mis-generated tool call cannot reach a dangerous tool.
        """
        if name in FORBIDDEN_TOOLS:
            raise MCPError("tool {0!r} is not available to the calendar assistant".format(name))
        if name not in ALLOWED_TOOLS:
            raise MCPError("tool {0!r} is not allow-listed for the calendar assistant".format(name))
        result = self._post("tools/call", {"name": name, "arguments": arguments or {}}, 4)
        return _decode_tool_result(result)

    def server_name(self) -> str:
        if not self._server_info:
            self.connect()
        return str(self._server_info.get("name", "unknown"))


def _decode_tool_result(result: Dict[str, Any]) -> Any:
    """Turn an MCP tool result into plain Python data.

    Tool results arrive as content blocks; the useful payload is the first
    ``text`` block. A block flagged ``isError`` is raised instead, because a
    failed search is a real failure the assistant should be told about.
    """
    blocks = result.get("content") or []
    if not isinstance(blocks, list):
        return blocks
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("isError"):
            raise MCPError(str(block.get("text", "tool call failed")))
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text", ""))
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None