# Claude Code & CLI Agent Integration Guide

This guide describes how to connect CLI-driven coding agents like **Claude Code**, **Codex CLI**, and **Gemini CLI** to `shake`.

---

## 1. Overview

CLI agents operate inside terminals or headless subprocesses. They can send and receive tasks via `shake` using:
1. **MCP Mode**: Launching `shake mcp` inside the Claude Code or Cursor environment.
2. **Channel / Shell Mode**: Running `shake wait` and `shake text` directly from scripts or shell tool calls.

---

## 2. Using with Claude Code

### Configuring MCP
Add `shake` to your Claude configuration (`~/.claude/claude_desktop_config.json` or project `.mcp.json`):

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

Claude will automatically discover the `shake_text`, `shake_wait`, and `shake_inbox` tools.

### Example Autonomous Flow
1. An external agent asks Claude Code to review a pull request:
   ```bash
   shake text <claude_id> "Review PR #42 in ~/repo and report build errors"
   ```
2. Claude Code receives the message, inspects the git repository, runs the tests, and replies:
   ```json
   {
     "name": "shake_text",
     "arguments": {
       "target_id": "1049281",
       "message": "PR #42 reviewed: 2 tests failed in test_auth.py. Diffs attached."
     }
   }
   ```
3. The response arrives within the guaranteed 60-second window.
