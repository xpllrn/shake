# Meta Muse Integration Guide

Meta Muse agents run in sandboxed cloud containers with strict network isolation. This guide explains how `shake` solves Muse's unique sandbox constraints.

---

## 1. Sandbox Constraints on Meta Muse

Meta Muse environments present two core architectural challenges:

1. **Inbound Connection Restrictions**: Muse containers have no public IP and cannot accept incoming TCP or HTTP connections. Standard webhooks and push mechanisms cannot reach them.
2. **Rotating Egress Proxies**: All outgoing network traffic passes through an egress proxy (default `hatch-egress-proxy:3128` for public internet or port `3130` for tunnel traffic). Muse mints a new proxy password for every subshell session, expiring old passwords in minutes and causing HTTP 407 (Proxy Authentication Required) errors if hardcoded.

---

## 2. How `shake` Solves Both Challenges

### Dynamic Proxy Resolution (`env.py`)
`shake` includes dynamic proxy resolution in [`env.py`](file:///Users/xpllrn/Documents/Projects/shake/src/shake/env.py). On every outbound network or relay operation, `shake` checks:
1. `$HTTPS_PROXY`
2. `$HTTP_PROXY`
3. `$ALL_PROXY`

It automatically combines the current shell's rotating username and password with the configured proxy destination. When a subshell rotates credentials, `shake` immediately picks up the new credentials without manual reconfiguration or HTTP 407 failures.

### The Pull-Only Polling Loop (`shake wait`)
Instead of requiring an open listening port, the Muse agent initiates an outbound pull loop:
```bash
shake wait --timeout 60
```
- While no messages are waiting, `shake wait` holds a light poll.
- The instant an envelope arrives, the command outputs the message payload as JSON and exits:
  ```json
  {"msg_id": "01M4VEWS...", "sender_id": "1049281", "body": "Please review the diff."}
  ```
- Muse processes the turn and replies outbound using `shake text <peer_id> <reply>`.
- Muse starts `shake wait` again for the next turn.

---

## 3. Step-by-Step Setup Inside Meta Muse

### Step 1: Initialize Identity
Inside the Muse container:
```bash
# Initialize directories and SQLite schema
shake init

# Create an agent identity
shake agent create "MuseDev"
# Output: Created agent 2094821

# Set as default for the project directory
shake agent use 2094821

# (Optional) Declare capabilities for capability routing
shake agent good-at "Python, Sandbox Execution, Code Analysis"
```

### Step 2: Connect Across Environments with Air-Gapped Tokens
Because Muse and your laptop or server are on separate machines without direct port forwarding, use signed Base64 tokens:

1. **On your laptop / external agent**:
   ```bash
   shake connect 2094821
   ```
   Outputs:
   ```text
   Share this token with the peer:
   jKF2AaZtc2dfaWS6MDFNNFZFN1pWVz...
   ```

2. **Inside Meta Muse**:
   ```bash
   shake connect --from-blob jKF2AaZtc2dfaWS6MDFNNFZFN1pWVz...
   ```
   Muse displays the 6-digit mutual code:
   ```text
   Mutual Verification Code: 395987
   ```

3. **Verify on Both Sides**:
   Confirm out-of-band that both sides show `395987`:
   - Laptop: `shake verify 2094821 395987`
   - Muse: `shake verify 1049281 395987`

Keys are now permanently pinned!

---

## 4. Running as an MCP Tool in Meta Muse

If your Meta Muse runtime supports **Model Context Protocol (MCP)**, register `shake` as a stdio server:

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

Muse will automatically have access to:
- `shake_wait`: Pull-loop for waiting on incoming tasks.
- `shake_text`: Send authenticated 1:1 messages.
- `shake_inbox`: Check message history.
- `shake_contacts`: Query verified peers by capabilities (`good_at`).
