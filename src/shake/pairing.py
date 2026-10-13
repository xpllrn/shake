"""Pairing state machine, signed connect protocol, mutual verification, and key pinning."""

import time
from typing import Any

import msgpack

from shake.config import ShakeConfig
from shake.crypto import KeyPair, compute_pairing_code, constant_time_compare, generate_nonce
from shake.protocol import Envelope, blob_to_envelope, envelope_to_blob, verify_envelope
from shake.store import KeyPinningViolationError, Store
from shake.ulid import generate_ulid

MAX_CONNECT_REQUESTS_PER_HOUR: int = 10
MAX_PENDING_INBOUND_REQUESTS: int = 50
PAIRING_SESSION_TTL_MS: int = 5 * 60 * 1000  # 5 minutes
MAX_VERIFY_ATTEMPTS: int = 3


class PairingError(Exception):
    """Base error for pairing and handshake failures."""
    pass


class RateLimitExceededError(PairingError):
    """Raised when rate limits on connect requests are exceeded."""
    pass


class SessionExpiredError(PairingError):
    """Raised when a pairing session exceeds the 5-minute TTL."""
    pass


class MaxAttemptsExceededError(PairingError):
    """Raised when 3 failed verification attempts terminate the session."""
    pass


class InvalidCodeError(PairingError):
    """Raised when an incorrect verification code is submitted."""
    pass


class BlockedContactError(PairingError):
    """Raised when interaction with a blocked contact is attempted."""
    pass


class PairingManager:
    """Orchestrates signed connect envelopes, SAS verification, and key pinning."""

    def __init__(self, store: Store, config: ShakeConfig | None = None) -> None:
        self.store = store
        self.config = config or ShakeConfig()

    def _load_agent_keypair(self, agent_id: str) -> KeyPair:
        key_path = self.config.key_file_path(agent_id)
        if not key_path.exists():
            raise PairingError(f"Private key for agent {agent_id} not found at {key_path}")
        return KeyPair.load(key_path)

    async def create_connect_request(
        self,
        local_agent_id: str,
        peer_id: str,
        peer_name: str = "",
    ) -> tuple[Envelope, str]:
        """Initiate outbound connection request. Returns signed Envelope and URL-safe token."""
        await self.store.cleanup_expired_sessions()
        now_ms = int(time.time() * 1000)

        # 1. Blocked check
        contact = await self.store.get_contact(local_agent_id, peer_id)
        if contact and contact.state == "blocked":
            raise BlockedContactError(f"Contact {peer_id} is blocked")

        # 2. Rate limit check
        recent_requests = await self.store.count_recent_connect_requests(local_agent_id)
        if recent_requests >= MAX_CONNECT_REQUESTS_PER_HOUR:
            raise RateLimitExceededError(
                f"Rate limit exceeded: max {MAX_CONNECT_REQUESTS_PER_HOUR} connects/hour"
            )

        # 3. Agent identity check
        local_agent = await self.store.get_agent(local_agent_id)
        if not local_agent:
            raise PairingError(f"Local agent {local_agent_id} not found in store")

        local_keypair = self._load_agent_keypair(local_agent_id)
        local_nonce = generate_nonce(32)
        session_id = generate_ulid()
        expires_at = now_ms + PAIRING_SESSION_TTL_MS

        # 4. Save local pairing session & contact (never touches peer's records!)
        await self.store.create_pairing_session(
            session_id=session_id,
            local_agent_id=local_agent_id,
            peer_id=peer_id,
            nonce_local=local_nonce,
            state="requested_out",
            expires_at=expires_at,
        )

        await self.store.create_or_update_contact(
            owner_agent_id=local_agent_id,
            peer_id=peer_id,
            name=peer_name or f"Agent {peer_id}",
            pubkey=contact.pubkey if (contact and contact.pinned_at) else b"",
            state="requested_out",
            pinned_at=contact.pinned_at if contact else None,
            good_at=contact.good_at if contact else "",
        )

        # 5. Build and sign connect_req envelope
        body_dict = {
            "name": local_agent.name,
            "pubkey": local_agent.pubkey,
            "nonce": local_nonce,
        }
        envelope = Envelope.create(
            type_="connect_req",
            from_id=local_agent_id,
            to_id=peer_id,
            body=msgpack.packb(body_dict, use_bin_type=True),
            nonce=local_nonce[:16],
        )
        envelope.sign(local_keypair)
        token = envelope_to_blob(envelope)
        return envelope, token

    async def handle_connect_request(
        self,
        local_agent_id: str,
        envelope: Envelope,
    ) -> tuple[Envelope, str, str]:
        """Receiver ingests signed connect_req envelope. Returns (ack_envelope, ack_token, code)."""
        await self.store.cleanup_expired_sessions()
        now_ms = int(time.time() * 1000)

        if envelope.type != "connect_req":
            raise PairingError(f"Expected envelope type connect_req, got {envelope.type}")
        if envelope.to_id != local_agent_id:
            raise PairingError(
                f"Envelope recipient {envelope.to_id} does not match {local_agent_id}"
            )

        sender_id = envelope.from_id
        if sender_id == local_agent_id:
            raise PairingError("An agent cannot connect to itself")

        # Unpack sender payload
        try:
            body = msgpack.unpackb(envelope.body, raw=False)
            sender_name = str(body["name"])
            sender_pubkey = bytes(body["pubkey"])
            sender_nonce = bytes(body["nonce"])
        except Exception as e:
            raise PairingError(f"Malformed connect_req body: {e}")

        # Verify signature against presented public key
        if not verify_envelope(envelope, sender_pubkey):
            raise PairingError("Invalid signature on connect_req envelope")

        # Check blocked
        contact = await self.store.get_contact(local_agent_id, sender_id)
        if contact and contact.state == "blocked":
            raise BlockedContactError(f"Sender {sender_id} is blocked")

        # Check key pinning
        if contact and contact.pinned_at is not None:
            if contact.pubkey != sender_pubkey:
                raise KeyPinningViolationError(
                    f"CRITICAL: Key pinning violation for {sender_id}! "
                    f"Sender presented public key {sender_pubkey.hex()[:12]}... "
                    f"differing from pinned key {contact.pubkey.hex()[:12]}..."
                )

        # Inbound queue rate limit
        inbound_count = await self.store.count_pending_inbound_requests(local_agent_id)
        if inbound_count >= MAX_PENDING_INBOUND_REQUESTS:
            raise RateLimitExceededError(
                f"Inbound queue full: max {MAX_PENDING_INBOUND_REQUESTS} pending requests"
            )

        local_agent = await self.store.get_agent(local_agent_id)
        if not local_agent:
            raise PairingError(f"Local agent {local_agent_id} not found in store")

        local_keypair = self._load_agent_keypair(local_agent_id)
        local_nonce = generate_nonce(32)
        expires_at = now_ms + PAIRING_SESSION_TTL_MS

        # Compute mutual symmetric SAS code
        code = compute_pairing_code(
            pubkey_a=local_agent.pubkey,
            nonce_a=local_nonce,
            pubkey_b=sender_pubkey,
            nonce_b=sender_nonce,
        )

        # Update local receiver's pairing session to pending_verify
        session_id = generate_ulid()
        await self.store.create_pairing_session(
            session_id=session_id,
            local_agent_id=local_agent_id,
            peer_id=sender_id,
            nonce_local=local_nonce,
            nonce_peer=sender_nonce,
            code=code,
            state="pending_verify",
            attempts=0,
            expires_at=expires_at,
        )

        # Update receiver's contact for sender (never touched sender's database!)
        await self.store.create_or_update_contact(
            owner_agent_id=local_agent_id,
            peer_id=sender_id,
            name=sender_name,
            pubkey=sender_pubkey,
            state="pending_verify",
            pinned_at=contact.pinned_at if contact else None,
            good_at=contact.good_at if contact else "",
        )

        # Build and sign connect_ack envelope
        ack_body = {
            "name": local_agent.name,
            "pubkey": local_agent.pubkey,
            "nonce": local_nonce,
        }
        ack_envelope = Envelope.create(
            type_="connect_ack",
            from_id=local_agent_id,
            to_id=sender_id,
            body=msgpack.packb(ack_body, use_bin_type=True),
            reply_to=envelope.msg_id,
            nonce=local_nonce[:16],
        )
        ack_envelope.sign(local_keypair)
        ack_token = envelope_to_blob(ack_envelope)
        return ack_envelope, ack_token, code

    async def handle_connect_ack(
        self,
        local_agent_id: str,
        envelope: Envelope,
    ) -> str:
        """Initiator ingests signed connect_ack envelope. Returns symmetric SAS code."""
        await self.store.cleanup_expired_sessions()
        now_ms = int(time.time() * 1000)

        if envelope.type != "connect_ack":
            raise PairingError(f"Expected envelope type connect_ack, got {envelope.type}")
        if envelope.to_id != local_agent_id:
            raise PairingError(
                f"Envelope recipient {envelope.to_id} does not match {local_agent_id}"
            )

        peer_id = envelope.from_id

        # Verify existing outbound session
        session = await self.store.get_pairing_session_by_peer(local_agent_id, peer_id)
        if not session:
            raise PairingError(f"No pairing session found for peer {peer_id}")
        if now_ms > session.expires_at:
            await self.store.delete_pairing_session(session.session_id)
            raise SessionExpiredError("Pairing session has expired (5 min TTL)")

        # Unpack ack payload
        try:
            body = msgpack.unpackb(envelope.body, raw=False)
            peer_name = str(body["name"])
            peer_pubkey = bytes(body["pubkey"])
            peer_nonce = bytes(body["nonce"])
        except Exception as e:
            raise PairingError(f"Malformed connect_ack body: {e}")

        # Verify signature
        if not verify_envelope(envelope, peer_pubkey):
            raise PairingError("Invalid signature on connect_ack envelope")

        # Check blocked
        contact = await self.store.get_contact(local_agent_id, peer_id)
        if contact and contact.state == "blocked":
            raise BlockedContactError(f"Peer {peer_id} is blocked")

        # Check key pinning
        if contact and contact.pinned_at is not None:
            if contact.pubkey != peer_pubkey:
                raise KeyPinningViolationError(
                    f"CRITICAL: Key pinning violation for {peer_id}! "
                    f"Peer presented public key {peer_pubkey.hex()[:12]}... "
                    f"differing from pinned key {contact.pubkey.hex()[:12]}..."
                )

        local_agent = await self.store.get_agent(local_agent_id)
        if not local_agent:
            raise PairingError(f"Local agent {local_agent_id} not found in store")

        # Compute mutual symmetric SAS code
        code = compute_pairing_code(
            pubkey_a=local_agent.pubkey,
            nonce_a=session.nonce_local,
            pubkey_b=peer_pubkey,
            nonce_b=peer_nonce,
        )

        # Update initiator's session to pending_verify
        await self.store.update_pairing_session(
            session_id=session.session_id,
            state="pending_verify",
            nonce_peer=peer_nonce,
            code=code,
            attempts=0,
        )

        # Update initiator's contact for peer
        await self.store.create_or_update_contact(
            owner_agent_id=local_agent_id,
            peer_id=peer_id,
            name=peer_name,
            pubkey=peer_pubkey,
            state="pending_verify",
            pinned_at=contact.pinned_at if contact else None,
            good_at=contact.good_at if contact else "",
        )
        return code

    async def process_connect_token(
        self,
        local_agent_id: str,
        token: str,
    ) -> dict[str, Any]:
        """Universal import handler for --from-blob token (request or ack)."""
        envelope = blob_to_envelope(token)
        if envelope.type == "connect_req":
            ack_env, ack_token, code = await self.handle_connect_request(local_agent_id, envelope)
            return {
                "state": "pending_verify",
                "code": code,
                "token": ack_token,
                "peer_id": envelope.from_id,
                "role": "receiver",
            }
        elif envelope.type == "connect_ack":
            code = await self.handle_connect_ack(local_agent_id, envelope)
            return {
                "state": "pending_verify",
                "code": code,
                "peer_id": envelope.from_id,
                "role": "initiator",
            }
        else:
            raise PairingError(f"Unsupported envelope type for connect token: {envelope.type}")

    async def initiate_connect(
        self,
        local_agent_id: str,
        peer_id: str,
        peer_name: str = "",
    ) -> dict[str, Any]:
        """High-level connect routing protocol steps independently without dual-writing."""
        req_env, req_token = await self.create_connect_request(
            local_agent_id=local_agent_id,
            peer_id=peer_id,
            peer_name=peer_name,
        )

        # Check if peer is also registered locally
        peer_agent = await self.store.get_agent(peer_id)
        if peer_agent:
            # Route connect_req to peer independently (no shortcut writing)
            ack_env, _ack_token, _peer_code = await self.handle_connect_request(peer_id, req_env)
            # Route connect_ack back to local agent independently
            code = await self.handle_connect_ack(local_agent_id, ack_env)
            return {
                "state": "pending_verify",
                "code": code,
                "token": req_token,
                "peer_id": peer_id,
            }

        return {
            "state": "requested_out",
            "token": req_token,
            "peer_id": peer_id,
        }

    async def verify_code(
        self,
        local_agent_id: str,
        peer_id: str,
        code: str,
    ) -> bool:
        """Verify the 6-digit mutual SAS code and pin peer's public key upon success."""
        now_ms = int(time.time() * 1000)

        session = await self.store.get_pairing_session_by_peer(local_agent_id, peer_id)
        if not session:
            raise PairingError(f"No active pairing session with peer {peer_id}")

        if now_ms > session.expires_at:
            await self.store.delete_pairing_session(session.session_id)
            await self.store.update_contact_state(local_agent_id, peer_id, "none")
            raise SessionExpiredError(
                "Pairing session expired (5 min TTL). Start over with `shake connect`."
            )

        if session.state != "pending_verify" or not session.code:
            raise PairingError(
                f"Session not ready for verification (current state: {session.state}). "
                "Both sides must exchange connection requests first."
            )

        # Constant-time comparison
        is_valid = constant_time_compare(code.strip(), session.code.strip())

        if not is_valid:
            attempts = session.attempts + 1
            if attempts >= MAX_VERIFY_ATTEMPTS:
                await self.store.delete_pairing_session(session.session_id)
                await self.store.update_contact_state(local_agent_id, peer_id, "none")
                raise MaxAttemptsExceededError(
                    f"Maximum verification attempts ({MAX_VERIFY_ATTEMPTS}) exceeded. "
                    "Pairing session locked and removed."
                )
            await self.store.update_pairing_session(session.session_id, attempts=attempts)
            raise InvalidCodeError(
                f"Incorrect verification code. Attempt {attempts} of {MAX_VERIFY_ATTEMPTS}."
            )

        # Success: pin key and mark verified
        contact = await self.store.get_contact(local_agent_id, peer_id)
        if not contact or not contact.pubkey:
            raise PairingError(f"Cannot verify peer {peer_id}: contact public key is missing")

        await self.store.update_contact_state(
            owner_agent_id=local_agent_id,
            peer_id=peer_id,
            state="verified",
            pinned_at=now_ms,
        )
        await self.store.delete_pairing_session(session.session_id)
        return True

    async def trust(self, owner_agent_id: str, peer_id: str) -> bool:
        """Promote a verified contact to trusted status."""
        contact = await self.store.get_contact(owner_agent_id, peer_id)
        if not contact:
            raise PairingError(f"Contact {peer_id} not found")
        if contact.state != "verified":
            raise PairingError(
                f"Contact must be verified before being trusted (current state: {contact.state})"
            )
        return await self.store.update_contact_state(owner_agent_id, peer_id, "trusted")

    async def block(self, owner_agent_id: str, peer_id: str) -> bool:
        """Block peer and purge active pairing session."""
        session = await self.store.get_pairing_session_by_peer(owner_agent_id, peer_id)
        if session:
            await self.store.delete_pairing_session(session.session_id)

        contact = await self.store.get_contact(owner_agent_id, peer_id)
        if contact:
            return await self.store.update_contact_state(owner_agent_id, peer_id, "blocked")
        else:
            await self.store.create_or_update_contact(
                owner_agent_id=owner_agent_id,
                peer_id=peer_id,
                name=f"Blocked {peer_id}",
                pubkey=b"",
                state="blocked",
            )
            return True

    async def unblock(self, owner_agent_id: str, peer_id: str) -> bool:
        """Unblock peer."""
        contact = await self.store.get_contact(owner_agent_id, peer_id)
        if not contact or contact.state != "blocked":
            return False
        new_state = "verified" if contact.pinned_at else "none"
        return await self.store.update_contact_state(owner_agent_id, peer_id, new_state)

    async def disconnect(self, owner_agent_id: str, peer_id: str) -> bool:
        """Remove contact, delete pinned key, and delete pairing sessions."""
        session = await self.store.get_pairing_session_by_peer(owner_agent_id, peer_id)
        if session:
            await self.store.delete_pairing_session(session.session_id)
        return await self.store.delete_contact(owner_agent_id, peer_id)
