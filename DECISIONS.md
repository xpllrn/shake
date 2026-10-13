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
