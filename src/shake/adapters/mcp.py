"""Model Context Protocol (MCP) server shim for Shake.

Exposes Shake commands as standard MCP JSON-RPC tools over stdio:
- shake_text: Send 1:1 message to an agent
- shake_inbox: Fetch incoming messages
- shake_wait: Wait for incoming message (pull loop for sandboxed agents)
- shake_contacts: List verified contacts
- shake_connect: Initiate or accept pairing request
- shake_verify: Verify 6-digit mutual SAS code and pin public key
"""

import json
import sys
import time
from typing import Any

from shake.client import ShakeClient
from shake.config import ShakeConfig


class ShakeMcpServer:
    """Stdio JSON-RPC server implementing the Model Context Protocol."""

    def __init__(self, config: ShakeConfig | None = None) -> None:
        self.config = config or ShakeConfig()
        self.client = ShakeClient(self.config)

    def _resolve_agent_id(self) -> str:
        proj_id = ShakeConfig.get_project_identity()
        if proj_id:
            return proj_id
        try:
            res = self.client.call_sync("agent_list")
            agents = res.get("data", {}).get("agents", [])
            if len(agents) == 1:
                return str(agents[0]["agent_id"])
        except Exception:
            pass
        return ""

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "shake_text",
                "description": "Send a secure, authenticated 1:1 message to another agent.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "target_id": {
                            "type": "string",
                            "description": "7-digit ID of the recipient agent",
                        },
                        "message": {
                            "type": "string",
                            "description": "Text message content to send",
                        },
                        "wait_timeout": {
                            "type": "number",
                            "description": "Timeout in seconds to wait for ACK (default 60)",
                            "default": 60,
                        },
                    },
                    "required": ["target_id", "message"],
                },
            },
            {
                "name": "shake_inbox",
                "description": "Retrieve incoming messages from the agent's inbox.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "after_msg_id": {
                            "type": "string",
                            "description": "Optional ULID cursor to fetch messages after",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of messages to fetch (default 20)",
                            "default": 20,
                        },
                    },
                },
            },
            {
                "name": "shake_wait",
                "description": (
                    "Block until an incoming message arrives or timeout expires. "
                    "Essential pull mechanism for sandboxed agents (such as Meta Muse) "
                    "that cannot receive incoming network connections."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "timeout": {
                            "type": "number",
                            "description": "Seconds to wait (default 60)",
                            "default": 60,
                        },
                    },
                },
            },
            {
                "name": "shake_contacts",
                "description": "List all contacts and their verification states.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "description": "Optional filter (e.g. 'verified', 'trusted')",
                        },
                    },
                },
            },
            {
                "name": "shake_connect",
                "description": (
                    "Initiate connection with a peer or accept a signed connection token."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "peer_id": {
                            "type": "string",
                            "description": "Peer agent ID to initiate connect with",
                        },
                        "token": {
                            "type": "string",
                            "description": "Base64 signed connect token received from peer",
                        },
                    },
                },
            },
            {
                "name": "shake_verify",
                "description": (
                    "Complete mutual verification by submitting the 6-digit SAS code and pin key."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "peer_id": {
                            "type": "string",
                            "description": "Peer agent ID",
                        },
                        "code": {
                            "type": "string",
                            "description": "6-digit verification code confirmed with the peer",
                        },
                    },
                    "required": ["peer_id", "code"],
                },
            },
        ]

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        local_id = self._resolve_agent_id()

        if name == "shake_text":
            target_id = str(args.get("target_id", "")).strip()
            message = str(args.get("message", "")).strip()
            wait = float(args.get("wait_timeout", 60.0))
            res = self.client.call_sync(
                "text",
                {
                    "sender_id": local_id,
                    "target_id": target_id,
                    "body": message,
                    "wait_timeout": wait,
                },
            )
            return {"status": "success", "data": res.get("data")}

        elif name == "shake_inbox":
            after = args.get("after_msg_id")
            limit = int(args.get("limit", 20))
            res = self.client.call_sync(
                "inbox",
                {
                    "local_agent_id": local_id,
                    "after_msg_id": after,
                    "limit": limit,
                },
            )
            return {"status": "success", "messages": res.get("data", {}).get("messages", [])}

        elif name == "shake_wait":
            timeout = float(args.get("timeout", 60.0))
            deadline = time.time() + timeout
            while time.time() < deadline:
                res = self.client.call_sync(
                    "inbox",
                    {"local_agent_id": local_id, "limit": 1},
                )
                messages = res.get("data", {}).get("messages", [])
                if messages:
                    return {"status": "message_received", "message": messages[0]}
                time.sleep(0.5)
            return {"status": "timeout", "message": None}

        elif name == "shake_contacts":
            state = args.get("state")
            res = self.client.call_sync(
                "contact_list",
                {"local_agent_id": local_id, "state": state},
            )
            return {"status": "success", "contacts": res.get("data", {}).get("contacts", [])}

        elif name == "shake_connect":
            peer_id = args.get("peer_id")
            token = args.get("token")
            res = self.client.call_sync(
                "connect",
                {
                    "local_agent_id": local_id,
                    "peer_id": peer_id or "",
                    "token": token or "",
                },
            )
            return {"status": "success", "data": res.get("data")}

        elif name == "shake_verify":
            peer_id = str(args.get("peer_id", "")).strip()
            code = str(args.get("code", "")).strip()
            res = self.client.call_sync(
                "verify",
                {
                    "local_agent_id": local_id,
                    "peer_id": peer_id,
                    "code": code,
                },
            )
            return {"status": "verified", "data": res.get("data")}

        else:
            raise ValueError(f"Unknown tool: {name}")

    def run_stdio(self) -> None:
        """Run JSON-RPC loop over standard input and standard output."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue

            req_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params", {})

            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "shake-mcp", "version": "0.1.0"},
                    },
                }
            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": self.list_tools()},
                }
            elif method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments", {})
                try:
                    tool_result = self.call_tool(tool_name, tool_args)
                    resp = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(tool_result)}]
                        },
                    }
                except Exception as e:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": f"Error: {e}"}],
                            "isError": True,
                        },
                    }
            elif method == "ping":
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }

            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
