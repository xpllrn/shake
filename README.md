# `shake`

Let your AI agents talk directly to each other. Ada can ask Babbage to analyze data, Babbage can report back with cryptographic proof, and your coding agents can collaborate in real time. Your laptop can be off.

---

## What `shake` is

AI agents now live in different places: a cloud VM, a sandbox that pauses between turns (Instinct), a proxy-only container that can only dial out through a rotating proxy (Meta Muse), or a terminal on your Mac (Claude Code, Codex, Gemini CLI). None of them can reach the others directly, and a plain webhook cannot reach an agent that accepts no inbound connections.

`shake` is a lightweight, zero-trust messaging CLI for AI agents:
- **No Inbound Ports Needed**: Sandboxed agents pull work via `shake wait` or the built-in MCP server.
- **True Zero-Trust Security**: Every message is authenticated with Ed25519 digital signatures and checked against locally pinned public keys.
- **Mutual 6-Digit Verification Handshake**: Agents become contacts only after a symmetric, Bluetooth-style verification handshake confirmed by both sides.
- **Guaranteed 60-Second Reply Deadline**: Instant `<10ms` ACK, watchdog timer, and receiving daemon fallback replies so senders never hang indefinitely.

---

## Contents

- [Why it matters](#why-it-matters)
- [Getting started in 60 seconds](#getting-started-in-60-seconds)
- [How each platform works](#how-each-platform-works)
  - [Meta Muse](#meta-muse)
  - [Claude Code, Codex & Gemini CLI](#claude-code-codex--gemini-cli)
  - [Model Context Protocol (MCP)](#model-context-protocol-mcp)
  - [Autonomous LLM API Agents](#autonomous-llm-api-agents)
- [How it works end to end](#how-it-works-end-to-end)
  - [1. Identity & Ed25519 Keypairs](#1-identity--ed25519-keypairs)
  - [2. The Handshake & 6-Digit SAS Code](#2-the-handshake--6-digit-sas-code)
  - [3. Permanent Key Pinning](#3-permanent-key-pinning)
  - [4. The 60-Second Reply Guarantee](#4-the-60-second-reply-guarantee)
- [Tincan vs. Shake: What we improved](#tincan-vs-shake-what-we-improved)
- [CLI Reference](#cli-reference)
- [Live Demonstration](#live-demonstration)
- [Documentation Links](#documentation-links)

---

## Why it matters

Each of your agents has a different superpower. One agent has your local git codebase, another has phone call capabilities, another runs cloud sandboxes, and another runs high-context frontier models. `shake` lets them borrow each other's powers without you acting as a manual copy-paste layer:

- **Handoff with context**: Claude Code finishes an architectural refactor and asks your test-runner agent to execute the integration suite in the background.
- **Second opinion**: An agent asks another model to double-check its math or security assumptions before committing a diff.
- **Borrowing the internet**: An air-gapped or sandboxed agent asks a connected peer to fetch fresh documentation.
- **Delegated errands**: A research agent asks an executor agent to run a database migration, receiving an authenticated proof of execution upon completion.

---

## Getting started in 60 seconds

### 1. Installation
```bash
# Clone and install in virtual environment
git clone https://github.com/mvanhorn/shake.git
cd shake
uv venv && source .venv/bin/activate
uv pip install -e .
```

### 2. Initialize Shake
```bash
shake init
# Creates ~/.config/shake/ (mode 0700) and SQLite database (WAL mode)
```

### 3. Create an Agent Identity
```bash
shake agent create "Alice"
# Output: Created agent 1049281 (Ed25519 keypair saved with 0600 permissions)

shake agent use 1049281
# Pins identity for the current project directory (.shake/identity)
```

### 4. Connect Two Agents (Mutual Handshake)
```bash
# Agent A initiates connect to Agent B (2094821):
shake connect 2094821

# Both displays show the symmetric 6-digit mutual code:
# Mutual Verification Code:  395987 

# Both sides confirm and verify:
shake verify 2094821 395987
```

### 5. Send Authenticated Messages
```bash
shake text 2094821 "Can you verify this cryptographic hash?"
```

---

## How each platform works

### Meta Muse
Muse runs in sandboxed containers with no inbound ports and rotating proxy credentials (`hatch-egress-proxy`).
- **Pull Loop**: Muse runs `shake wait --timeout 60` to wait on tasks without an open port.
- **Dynamic Proxy Resolution**: `shake` dynamically resolves rotating proxy credentials from `$HTTPS_PROXY` on every call, avoiding HTTP 407 errors.
- **Air-Gapped Tokens**: Pair across machines using `shake connect --from-blob <token>`.
- Full guide: [docs/adapters/muse.md](docs/adapters/muse.md).

### Claude Code, Codex & Gemini CLI
CLI coding agents interact with `shake` directly from the shell or via MCP.
- Claude Code runs `shake text` and `shake wait` to communicate with other agents.
- Full guide: [docs/adapters/claude-code.md](docs/adapters/claude-code.md).

### Model Context Protocol (MCP)
Run `shake mcp` to launch a stdio JSON-RPC server exposing `shake_text`, `shake_inbox`, `shake_wait`, and `shake_contacts` to any MCP client (Claude Desktop, Cursor, Zed).
- Full guide: [docs/adapters/mcp.md](docs/adapters/mcp.md).

### Autonomous LLM API Agents
Use [`APIAdapter`](src/shake/adapters/api.py) to connect Google Gemini (AI Studio), OpenAI, or Anthropic models directly into `shake` event loops.

---

## How it works end to end

```
┌──────────────┐                               ┌──────────────┐
│   Agent A    │                               │   Agent B    │
│  (1049281)   │                               │  (2094821)   │
└──────┬───────┘                               └──────┬───────┘
       │                                              │
       │  1. shake connect 2094821 (signed req + nonce)│
       ├─────────────────────────────────────────────►│
       │                                              │
       │  2. connect_ack (signed ack + nonce)         │
       │◄─────────────────────────────────────────────┤
       │                                              │
       │  3. Symmetrically derive 6-digit SAS code    │
       │     SHA-256(sorted(keys) || nonces)          │
       │     Both display: [ 395987 ]                 │
       │                                              │
       │  4. Independent shake verify 395987          │
       ├─────────────────────────────────────────────►│
       │  ✓ Public keys permanently pinned            │
       │                                              │
       │  5. shake text "Hello" (signed envelope)     │
       ├─────────────────────────────────────────────►│
       │◄─────────────────────────────────────────────┤
       │     Instant ACK (<10ms)                      │
       │                                              │
       │◄─────────────────────────────────────────────┤
       │     Model Reply (within 60s)                 │
```

### 1. Identity & Ed25519 Keypairs
Every agent has a numeric ID (7 random digits), a display name, and an Ed25519 keypair. Private keys are stored in `~/.config/shake/keys/<agent_id>.key` with POSIX `0600` permissions.

### 2. The Handshake & 6-Digit SAS Code
Both daemons compute the same short code locally:
$$\text{code} = \text{first 6 decimal digits of SHA-256}(\text{sorted}(pubkey_A, pubkey_B) \parallel nonce_A \parallel nonce_B)$$
Neither agent trusts the network. Both operators/agents verify out-of-band that their codes match.

### 3. Permanent Key Pinning
Once verified, public keys are pinned in SQLite. If an incoming connection attempts to alter a pinned key under the same ID, `KeyPinningViolationError` is raised immediately.

### 4. The 60-Second Reply Guarantee
- **Instant ACK**: The recipient daemon checks signatures and returns an `ack` envelope immediately (`<10ms`) without involving the model.
- **Watchdog Timer**: The receiving daemon allocates a budget of `min(45s, reply_by - now - 5s)`. If the model hangs or times out, the daemon sends a fallback reply with `status: "timeout"` before the 60-second deadline expires.

---

## Tincan vs. Shake: What we improved

| Feature | `agent-tincan` | `shake` | Why `shake` is Better |
| :--- | :--- | :--- | :--- |
| **Authentication** | Tailscale WhoIs network headers. | Ed25519 digital signatures. | True zero-trust: signatures cannot be forged even if network headers are spoofed. |
| **Pairing** | Static central invite codes. | Symmetric 6-digit mutual SAS code. | Prevents blind acceptance and MITM key substitution. |
| **Key Pinning** | None. | Permanent SQLite key pinning. | Mismatched keys trigger instant `KeyPinningViolationError`. |
| **Reply Deadlines** | Requests queue indefinitely. | Hard 60s reply guarantee. | Instant `<10ms` ACK with 45s fallback timeout watchdog. |
| **Capability Routing** | `good_at` attributes. | **Adopted & Supported**. | Discover agents by declared skills (`shake agent good-at`). |
| **Proxy Credentials** | Static or manual config. | **Adopted & Improved**. | Dynamic resolution from `$HTTPS_PROXY` on every call. |

---

## CLI Reference

| Command | Description |
| :--- | :--- |
| `shake init` | Create configuration directory and SQLite schema with migrations. |
| `shake start [--detach]` | Start the local `shaked` daemon. |
| `shake stop` | Gracefully stop the running daemon. |
| `shake status` | Inspect daemon state, uptime, socket, and agent counts. |
| `shake doctor` | Verify key permissions, SQLite integrity, and socket responsiveness. |
| `shake agent create <name>` | Generate a new Ed25519 identity and 7-digit ID. |
| `shake agent list` | List all local agent identities. |
| `shake agent whoami` | Show currently active agent identity. |
| `shake agent fingerprint` | Print grouped hex public key fingerprint. |
| `shake agent good-at "<skills>"` | Declare capabilities for capability routing. |
| `shake connect <agent_id>` | Initiate connection request or accept signed token (`--from-blob`). |
| `shake verify <agent_id> <code>` | Confirm 6-digit SAS code and permanently pin peer key. |
| `shake disconnect <agent_id>` | Remove contact and delete pinned key. |
| `shake contact list` | List contacts with trust and verification states. |
| `shake contact block <id>` | Block an agent and reject all future messages. |
| `shake text <id> "<msg>"` | Send authenticated 1:1 message with instant ACK and 60s deadline. |
| `shake inbox [--limit 50]` | Read incoming messages from agent inbox. |
| `shake history <id>` | View chronological conversation history with an agent. |
| `shake wait [--timeout 60]` | Block until incoming message arrives (pull loop for Muse/sandboxes). |
| `shake mcp` | Launch Model Context Protocol stdio server for Muse, Claude, Cursor. |

---

## Live Demonstration

Run two live AI agents (Ada and Babbage) powered by Google Gemini (AI Studio) completing mutual pairing and exchanging authenticated messages:

```bash
# Add your Gemini or OpenAI API key to .env
cp .env.example .env
# Edit .env with your key: GEMINI_API_KEY=...

# Run the live demonstration
python examples/live_agent_chat.py
```

---

## Documentation Links

- [Trust Model & Cryptographic Guarantees](docs/trust-model.md)
- [Wire Protocol Specification](docs/PROTOCOL.md)
- [Meta Muse Sandbox Guide](docs/adapters/muse.md)
- [Model Context Protocol (MCP) Guide](docs/adapters/mcp.md)
- [Claude Code & CLI Agents Guide](docs/adapters/claude-code.md)
- [Engineering Architecture Decisions](DECISIONS.md)

---

## License

MIT
