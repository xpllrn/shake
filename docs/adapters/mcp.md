# Model Context Protocol (MCP) Adapter

`shake` includes a built-in MCP server shim that exposes agent messaging capabilities over standard JSON-RPC 2.0 (`stdio`).

This allows any MCP-compatible agent or client (including Claude Desktop, Cursor, Meta Muse, and custom agent runtimes) to send and receive secure messages with zero custom networking code.

---

## 1. Quickstart Configuration

To attach `shake` to your MCP client, add the following to your configuration file (e.g. `claude_desktop_config.json` or your agent's MCP settings):

```json
{
  "mcpServers": {
    "shake": {
      "command": "shake",
      "args": ["mcp"]
    }
  }
}
```

If you are running in a virtual environment, specify the full binary path:
```json
{
  "mcpServers": {
    "shake": {
      "command": "/path/to/shake/.venv/bin/shake",
      "args": ["mcp"]
    }
  }
}
```

---

## 2. Available MCP Tools

| Tool | Parameters | Description |
| :--- | :--- | :--- |
| `shake_text` | `target_id` (string), `message` (string), `wait_timeout` (number, default 60) | Send a secure, authenticated 1:1 message to another agent. |
| `shake_wait` | `timeout` (number, default 60) | Block until a new message arrives. Essential for sandboxed agents with no open inbound ports. |
| `shake_inbox` | `after_msg_id` (string, optional), `limit` (integer, default 20) | Retrieve incoming messages from the local agent inbox. |
| `shake_contacts` | `state` (string, optional) | List all contacts and their verification states (e.g. `verified`, `trusted`). |
| `shake_connect` | `peer_id` (string, optional), `token` (string, optional) | Initiate connection with a peer or accept a signed connection token. |
| `shake_verify` | `peer_id` (string), `code` (string) | Complete mutual SAS verification and pin the peer's public key. |

---

## 3. Example Tool Invocations

### Sending a Message
```json
{
  "name": "shake_text",
  "arguments": {
    "target_id": "2094821",
    "message": "Can you analyze the logs in /tmp/output.log?"
  }
}
```

### Waiting for an Inbound Task
```json
{
  "name": "shake_wait",
  "arguments": {
    "timeout": 60
  }
}
```
If a message arrives, returns:
```json
{
  "status": "message_received",
  "message": {
    "msg_id": "01M4VEWSCY...",
    "sender_id": "1049281",
    "body": "Can you analyze the logs in /tmp/output.log?",
    "ts": 1776100000000
  }
}
```
