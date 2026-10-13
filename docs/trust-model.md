# Shake Trust Model & Cryptographic Guarantees

`shake` is designed under a **Zero-Trust** security architecture for autonomous AI agents. Unlike systems that rely on ambient network trust, unauthenticated HTTP headers, or network-level IP attribution, `shake` guarantees end-to-end cryptographic integrity, mutual identity verification, and strict message authenticity.

---

## 1. What `shake` Guarantees

1. **Cryptographic Identity**: Every agent identity is defined by an Ed25519 asymmetric keypair. A 7-digit numeric agent ID (e.g. `1049281`) serves as a public routing handle, but the private key stored at `~/.config/shake/keys/<agent_id>.key` (POSIX permissions `0600`) defines the true cryptographic identity.
2. **Mutual Out-of-Band SAS Verification**: Agents cannot become contacts through passive acceptance or blind invite links. Both operators/agents must independently verify a symmetric 6-digit Short Authentication String (SAS) code derived from their sorted public keys and cryptographic nonces.
3. **Immutable Public Key Pinning**: Once verified, a peer's public key is pinned permanently in local SQLite storage. Even if an attacker attempts to reconnect or upsert using the same `peer_id`, any mismatch between the incoming key and the pinned key triggers an immediate `KeyPinningViolationError`. The SQLite database utilizes a conditional `CASE` expression preventing overwrites even under race conditions.
4. **Local Signature Verification**: Every MessagePack envelope is signed with the sender's Ed25519 private key over its canonical serialization. The receiving daemon verifies this signature directly against the pinned public key before accepting the envelope.
5. **Replay & Clock Skew Protection**:
   - Envelopes with timestamps (`ts`) outside a ±5 minute window are rejected (`StaleTimestampError`).
   - Every envelope contains an ephemeral 32-byte cryptographic nonce. Seen nonces are cached and rejected if observed again within the TTL window (`ReplayAttackError`).
6. **Isolation Across Hosts and Agents**: Contacts and messages are scoped strictly by `(owner_agent_id, peer_id)`. Two agents hosted on the same machine run the exact same protocol code as agents on separate machines across the internet, completely eliminating host-local backdoor shortcuts or dual-writing bugs.
7. **"Data, Not Instructions" Principle**: Incoming peer messages are strictly data payloads. The runtime never routes message content directly to shell execution without explicit local confirmation.

---

## 2. Comparison: `shake` vs. `tincan`

| Security Dimension | `agent-tincan` Approach | `shake` Zero-Trust Approach |
| :--- | :--- | :--- |
| **Authentication** | Tailscale `WhoIs` / unauthenticated network headers. | Ed25519 digital signatures verified locally on every envelope. |
| **Pairing & Verification** | Central invite codes with no mutual cryptographic proof. | Symmetrically derived 6-digit SAS code with independent verification by both sides. |
| **Key Tampering / MITM** | No key pinning. Peers can rotate keys or be intercepted if network trust fails. | Strict Ed25519 key pinning. Key mutation raises `KeyPinningViolationError`. |
| **Brute-Force Defense** | No verification attempt limits. | 5-minute session TTL and permanent session lockout after 3 failed SAS attempts. |
| **Denial of Service** | Unbounded connection requests. | Rate limit of 10 outbound connects/hr and maximum 50 pending inbound requests. |
| **Reply Guarantees** | Requests can queue indefinitely without hard timeouts. | Enforced instant `<10ms` ACK + 60s hard reply deadline with 45s fallback timeout. |

---

## 3. The Short Authentication String (SAS) Protocol

Pairing between Agent A ($A$) and Agent B ($B$) proceeds through ephemeral key-exchange nonces:

1. **Initiation**: Agent A generates a 32-byte random nonce $N_A$ and creates a signed `connect_req` envelope carrying $(A_{id}, name_A, pubkey_A, N_A)$.
2. **Response**: Agent B receives the request, generates a 32-byte nonce $N_B$, and replies with a signed `connect_ack` envelope carrying $(B_{id}, name_B, pubkey_B, N_B)$.
3. **Symmetric Code Computation**: Both agents lexicographically sort the public keys:
   $$\text{Pair} = \text{sort}( (pubkey_A, N_A), (pubkey_B, N_B) )$$
   $$\text{Digest} = \text{SHA-256}( pubkey_{\text{first}} \parallel pubkey_{\text{second}} \parallel N_{\text{first}} \parallel N_{\text{second}} )$$
   $$\text{SAS Code} = \text{first 6 decimal digits of Digest}$$
4. **Independent Verification**: Both operators/agents verify out-of-band that their local displays show the exact same 6-digit code. Both run `shake verify <peer_id> <code>`. Once verified, keys are pinned.

---

## 4. Threat Model & Mitigations

### Malicious Relay or Network Intermediary
- **Threat**: An intermediary modifies or injects message payloads.
- **Mitigation**: Every message is signed with the sender's private key. Modification breaks the Ed25519 signature. The local daemon verifies signatures with the pinned key, rejecting tampered messages instantly.

### Man-in-the-Middle (MITM) During Connect
- **Threat**: An attacker intercepts the initial handshake and substitutes their own public key.
- **Mitigation**: The derived 6-digit SAS code depends directly on the unalterable public key bytes and nonces. If the attacker substitutes keys, the 6-digit codes computed on each end will diverge. Both operators must confirm matching codes out-of-band before pinning occurs.

### Replay Attacks
- **Threat**: An eavesdropper captures an authentic envelope and replays it later.
- **Mitigation**: Timestamps must fall within 5 minutes of current time. Ephemeral nonces are stored in SQLite and deduplicated. Replayed messages trigger `ReplayAttackError`.

### Rogue Prompts & Remote Code Execution
- **Threat**: A compromised peer sends a prompt injection instructing the receiving agent to execute `rm -rf /` or leak private keys.
- **Mitigation**: The agent adapter wraps peer inputs with clear isolation boundaries. Private keys never leave `~/.config/shake/keys/` and are never exposed in message contexts.
