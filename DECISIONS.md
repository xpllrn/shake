# Engineering Decisions

This document records architectural and implementation decisions for `shake`.

## 1. Package Management & Tooling
- **Decision**: Managed with `uv` and Python 3.12+. Built with `hatchling`.
- **Rationale**: Meets Python 3.12+ requirement, provides ultra-fast virtualenv management and deterministic dependency resolution.

## 2. Cryptography & Identification
- **Decision**: Ed25519 using `PyNaCl` (`SigningKey`, `VerifyKey`).
- **Identity format**: 7-digit random integer (`1000000`–`9999999`) formatted as a string for `agent_id`; 6-digit random integer (`100000`–`999999`) for `room_id`.
- **Public Key Fingerprint**: Grouped hex string derived from SHA-256 of the 32-byte Ed25519 public key (e.g. `aa:bb:cc:dd:...`).
- **Pairing Code Derivation**: `first 6 digits of decimal string SHA-256(sorted(pubkeyA, pubkeyB) || nonceA || nonceB)`.
- **Key Storage**: Private keys are stored in `~/.config/shake/keys/<agent_id>.key` with POSIX mode `0600`. The directory is created with mode `0700`.

## 3. Envelope Serialization & Canonical Hashing
- **Decision**: MessagePack (`msgpack`) with explicit deterministic dictionary serialization for envelope signing.
- **Canonical Envelope Representation**: When computing or checking Ed25519 signatures, the `sig` field is excluded and the remaining envelope fields are packed in a deterministic, sorted-key dictionary format so verification is identical across platforms and runtimes.
- **ULID for Message IDs**: Generated using Crockford's Base32 encoding (48-bit millisecond timestamp + 80-bit cryptographic randomness) providing chronological sorting, high entropy, and collision resistance without requiring an external C extension.

## 4. Local IPC & Daemon Architecture
- **Decision**: Local daemon `shaked` communicates via Unix domain socket (`~/.config/shake/shaked.sock`).
- **Framing**: Length-prefixed MessagePack frames (4-byte big-endian payload length followed by msgpack payload).
- **Transport Abstraction**: Abstract `Transport` base class in `shake.transport.base` to allow swapping Unix domain socket with NATS (or others) later without touching core business logic or envelopes.

## 5. Storage
- **Decision**: SQLite in WAL mode (`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;`) accessed asynchronously via `aiosqlite` and synchronously during bootstrapping.
- **Migrations**: Version-controlled migrations runner tracked in `schema_migrations`.
- **Indexing**: Composite indexes on `(target_id, ts)` and `(sender_id, ts)`.

## 6. AF_UNIX Path Length Safeguard
- **Decision**: When `socket_path` exceeds 95 characters (e.g. deeply nested temp directories on macOS where Darwin restricts `sockaddr_un` to 104 bytes), `ShakeConfig.socket_path` deterministically falls back to `/tmp/shaked_<sha256(home)[:8]>.sock`.
- **Rationale**: Guarantees rock-solid reliability across all macOS CI/test runners without impacting default `~/.config/shake/shaked.sock`.

## 7. Handshake Code Canonical Symmetry
- **Decision**: `compute_pairing_code` lexicographically orders `(pubkey, nonce)` tuples based on public key bytes: `sorted(pubkeyA, pubkeyB) || nonceA || nonceB` where `nonce` travels with its corresponding `pubkey`.
- **Rationale**: Guarantees that both initiator and responder compute the exact same 6-digit decimal code without relying on transport/broker state or asymmetric roles.

## 8. Multi-Agent Ecosystem Compatibility (Meta Muse, Claude Code, Codex, MCP)
- **Decision**: The entire core runtime, identity management, envelope validation, and routing layer are 100% deterministic Python without LLM dependencies.
- **Rationale**: Agents from any runtime (Meta Muse, Claude Code, Codex CLI, Gemini CLI, custom LLM loops, or MCP tools) communicate over standard CLI / JSON or Unix socket without runtime impedance.

## 9. Handshake State Machine & Mutual Verification
- **Decision**: Contacts transition through explicit states: `none` -> `requested_out` / `requested_in` -> `pending_verify` -> `verified` -> `trusted` (or `blocked`).
- **Code verification**: Both agents compute a 6-digit symmetric code derived from their sorted public keys and 32-byte nonces. Verification is evaluated in constant time (`nacl.bindings.sodium_memcmp`).
- **Brute-Force Mitigation**: Sessions are locked after 3 failed verification attempts and automatically expire after 5 minutes (300 seconds).
- **Key Pinning Enforcement**: Once verified, a peer's public key is permanently pinned. If a peer attempts to re-verify or reconnect with a different public key under the same `peer_id`, `KeyPinningViolationError` is raised immediately.

## 10. Rate Limiting & Denial-of-Service Defense
- **Decision**: Connect requests are throttled at two levels:
  1. Maximum 10 outbound connect requests per hour per local agent.
  2. Maximum 50 pending inbound requests in the local agent's queue.
- **Rationale**: Prevents malicious or misconfigured agents from spamming connection requests or exhausting SQLite storage.

## 11. Tincan Comparison: Patterns Adopted & Limits Fixed
- **Adopted Pattern 1: Capability Routing (`good_at`)**:
  - Agents and contacts declare what tasks or skills they excel at via `good_at` (e.g. `shake agent good-at "Python, Docker"`).
  - Contacts can be queried with `good_at` capabilities for intelligent agent dispatch.
- **Adopted Pattern 2: Dynamic Egress Proxy Handling for Meta Muse**:
  - Sandboxed agents running behind corporate or sandbox egress proxies (e.g. `hatch-egress-proxy:3128` with rotating credentials) require fresh environment resolution. `src/shake/env.py` dynamically extracts valid proxy credentials on demand.
- **Fixed Limit 1: Zero-Trust Security vs Unauthenticated Network Headers**:
  - Tincan relied on unauthenticated network headers / Tailscale WhoIs without message authentication. Shake signs every envelope with Ed25519, verifies signatures locally with pinned public keys, and rejects all untrusted peers.
- **Fixed Limit 2: Mutual 6-Digit Verification vs Central Invite Codes**:
  - Tincan used static invite codes. Shake uses an ephemeral, symmetric 6-digit out-of-band verification handshake with cryptographic nonces and a 3-attempt lockout.
- **Fixed Limit 3: Guaranteed 60-Second Reply Deadline**:
  - Tincan queued requests indefinitely. Shake enforces an instant ACK and a hard 60-second reply deadline with receiving runtime timeout fallback at 45 seconds.
## 12. Scoped Contacts & Pinned Key Immutability
- **Decision**: `contacts` table is keyed by `(owner_agent_id, peer_id)`. `pairing_sessions` table has a UNIQUE index on `(local_agent_id, peer_id)`.
- **Key Pinning Rule**: A pinned public key (`pinned_at IS NOT NULL`) is strictly immutable. If an incoming connection attempts to upsert or alter an existing pinned public key with a mismatched key, `KeyPinningViolationError` is raised immediately, and the SQL upsert utilizes `CASE WHEN contacts.pinned_at IS NOT NULL THEN contacts.pubkey ELSE excluded.pubkey END`.

## 13. Elimination of Dual-Writing & Network Symmetry
- **Decision**: The daemon and CLI never mutate the peer agent's records locally, even if the peer happens to reside in the same local database.
- **Rationale**: Treats two agents on the same host identically to two agents on separate machines across the internet. Prevents shortcut vulnerabilities and architectural divergence when remote transports (NATS, WebRTC, relay) are used.

## 14. Signed Connect Envelopes & Offline Exchange
- **Decision**: Connection requests (`connect_req`) and acknowledgments (`connect_ack`) are packaged as canonical, Ed25519-signed MessagePack envelopes.
- **Air-gapped / File Transport**: Connection tokens are URL-safe base64 strings containing the signed envelope. Operators or autonomous runtimes can exchange them directly via `--from-blob <token>` or `--from-file <path>`.

## 15. Independent Mutual SAS Verification UX
- **Decision**: The CLI displays the computed 6-digit SAS code, explains the security rationale, and explicitly instructs the operator to verify that the peer displays the exact same code out-of-band.
- **No Pre-filling**: The verification command is never pre-filled with the local code in prompts. Both agents/operators must independently verify the session by running `shake verify <peer_id> <code>` using the code confirmed with their peer.

