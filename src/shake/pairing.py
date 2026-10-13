"""Pairing state machine, mutual handshake verification, key pinning, and rate limits."""

import time
from typing import Any

from shake.crypto import compute_pairing_code, constant_time_compare, generate_nonce
from shake.store import Store
from shake.ulid import generate_ulid

MAX_CONNECT_REQUESTS_PER_HOUR: int = 30
MAX_PENDING_INBOUND_REQUESTS: int = 10
PAIRING_SESSION_TTL_MS: int = 5 * 60 * 1000  # 5 minutes
MAX_VERIFY_ATTEMPTS: int = 3


class PairingError(Exception):
    """Base error for pairing and handshake failures."""
    pass


class KeyPinningViolationError(PairingError):
    """Raised when an agent presents a key differing from its pinned public key."""
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
    """Orchestrates handshake pairing sessions, verification, and key pinning."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def initiate_connect(
        self,
        local_agent_id: str,
        peer_id: str,
        peer_name: str = "",
        peer_pubkey: bytes | None = None,
    ) -> dict[str, Any]:
        """Initiate or accept a connection request between two agents."""
        await self.store.cleanup_expired_sessions()
        now_ms = int(time.time() * 1000)

        # 1. Check if contact is blocked
        contact = await self.store.get_contact(peer_id)
        if contact and contact.state == "blocked":
            raise BlockedContactError(f"Contact {peer_id} is blocked")

        # 2. Check Key Pinning on existing verified/trusted contacts
        if contact and contact.pinned_at is not None and peer_pubkey:
            if contact.pubkey != peer_pubkey:
                raise KeyPinningViolationError(
                    f"CRITICAL: Key pinning violation for agent {peer_id}! "
                    f"Peer presented a public key differing from the pinned key. "
                    f"Possible man-in-the-middle or key replacement attack."
                )

        # 3. Check rate limits for outbound connect requests
        recent_requests = await self.store.count_recent_connect_requests(local_agent_id)
        if recent_requests >= MAX_CONNECT_REQUESTS_PER_HOUR:
            raise RateLimitExceededError(
                f"Rate limit exceeded: max {MAX_CONNECT_REQUESTS_PER_HOUR} connects/hour"
            )

        # 4. Check if peer exists locally in registry or as contact
        local_agent = await self.store.get_agent(local_agent_id)
        if not local_agent:
            raise PairingError(f"Local agent {local_agent_id} not found")

        peer_agent = await self.store.get_agent(peer_id)
        if peer_agent:
            peer_pubkey = peer_pubkey or peer_agent.pubkey
            peer_name = peer_name or peer_agent.name

        # Check for existing pending session from peer (accepting inbound request)
        inbound_session = await self.store.get_pairing_session_by_peer(
            local_agent_id=peer_id, peer_id=local_agent_id
        )

        if inbound_session and inbound_session.state in ("requested_out", "requested_in"):
            # Check inbound expiry
            if now_ms > inbound_session.expires_at:
                await self.store.delete_pairing_session(inbound_session.session_id)
                raise SessionExpiredError("Inbound pairing session has expired (5 min TTL)")

            # Peer already initiated: we accept!
            local_nonce = generate_nonce(32)
            peer_nonce = inbound_session.nonce_local

            if not peer_pubkey:
                raise PairingError("Cannot compute code: peer public key is missing")

            # Compute symmetric 6-digit verification code
            code = compute_pairing_code(
                pubkey_a=local_agent.pubkey,
                nonce_a=local_nonce,
                pubkey_b=peer_pubkey,
                nonce_b=peer_nonce,
            )

            # Update inbound session to pending_verify
            await self.store.update_pairing_session(
                session_id=inbound_session.session_id,
                state="pending_verify",
                nonce_peer=local_nonce,
                code=code,
                attempts=0,
            )

            # Also create/update local pairing session for local agent
            local_session_id = generate_ulid()
            await self.store.create_pairing_session(
                session_id=local_session_id,
                local_agent_id=local_agent_id,
                peer_id=peer_id,
                nonce_local=local_nonce,
                nonce_peer=peer_nonce,
                code=code,
                state="pending_verify",
                attempts=0,
                expires_at=inbound_session.expires_at,
            )

            # Update contacts on both sides to pending_verify
            await self.store.create_or_update_contact(
                peer_id=peer_id,
                name=peer_name or f"Agent {peer_id}",
                pubkey=peer_pubkey,
                state="pending_verify",
            )
            await self.store.create_or_update_contact(
                peer_id=local_agent_id,
                name=local_agent.name,
                pubkey=local_agent.pubkey,
                state="pending_verify",
            )

            return {
                "state": "pending_verify",
                "code": code,
                "peer_id": peer_id,
                "session_id": local_session_id,
                "expires_at": inbound_session.expires_at,
            }

        # 5. Peer has not initiated yet: this is a fresh outbound request
        # Check target inbound limit if target is local
        if peer_agent:
            pending_inbound = await self.store.count_pending_inbound_requests(peer_id)
            if pending_inbound >= MAX_PENDING_INBOUND_REQUESTS:
                raise RateLimitExceededError(
                    f"Target {peer_id} exceeded max pending ({MAX_PENDING_INBOUND_REQUESTS})"
                )

        local_nonce = generate_nonce(32)
        expires_at = now_ms + PAIRING_SESSION_TTL_MS
        session_id = generate_ulid()

        await self.store.create_pairing_session(
            session_id=session_id,
            local_agent_id=local_agent_id,
            peer_id=peer_id,
            nonce_local=local_nonce,
            state="requested_out",
            expires_at=expires_at,
        )

        await self.store.create_or_update_contact(
            peer_id=peer_id,
            name=peer_name or (peer_agent.name if peer_agent else f"Agent {peer_id}"),
            pubkey=peer_pubkey or (peer_agent.pubkey if peer_agent else b""),
            state="requested_out",
        )

        # If peer is local on this machine, also register peer's inbound session
        if peer_agent:
            peer_session_id = generate_ulid()
            await self.store.create_pairing_session(
                session_id=peer_session_id,
                local_agent_id=peer_id,
                peer_id=local_agent_id,
                nonce_local=local_nonce,  # carries initiator nonce
                state="requested_in",
                expires_at=expires_at,
            )
            await self.store.create_or_update_contact(
                peer_id=local_agent_id,
                name=local_agent.name,
                pubkey=local_agent.pubkey,
                state="requested_in",
            )

        return {
            "state": "requested_out",
            "peer_id": peer_id,
            "session_id": session_id,
            "expires_at": expires_at,
        }

    async def verify_code(
        self,
        local_agent_id: str,
        peer_id: str,
        code: str,
    ) -> dict[str, Any]:
        """Verify the 6-digit mutual code for an active pairing session."""
        session = await self.store.get_pairing_session_by_peer(local_agent_id, peer_id)
        if not session:
            raise PairingError(f"No pairing session found with peer {peer_id}")

        now_ms = int(time.time() * 1000)

        # 1. Check expiration (5 minutes)
        if now_ms > session.expires_at:
            await self.store.delete_pairing_session(session.session_id)
            await self.store.update_contact_state(peer_id, "none")
            raise SessionExpiredError("Pairing session expired (5 min TTL). Please connect again.")

        # 2. Check maximum attempts
        if session.attempts >= MAX_VERIFY_ATTEMPTS:
            await self.store.delete_pairing_session(session.session_id)
            await self.store.update_contact_state(peer_id, "none")
            raise MaxAttemptsExceededError(
                f"Max verify attempts ({MAX_VERIFY_ATTEMPTS}) exceeded. Session terminated."
            )

        # 3. If session does not have code yet, it hasn't been accepted by both sides
        if not session.code:
            raise PairingError(
                f"Connection request is still pending acceptance by peer {peer_id}"
            )

        # 4. Constant-time comparison of the 6-digit code
        is_valid = constant_time_compare(code.strip(), session.code.strip())

        if not is_valid:
            new_attempts = session.attempts + 1
            if new_attempts >= MAX_VERIFY_ATTEMPTS:
                await self.store.delete_pairing_session(session.session_id)
                await self.store.update_contact_state(peer_id, "none")
                raise MaxAttemptsExceededError(
                    "Verification failed: 3 incorrect attempts reached. Session terminated."
                )

            await self.store.update_pairing_session(
                session_id=session.session_id,
                attempts=new_attempts,
            )
            rem = MAX_VERIFY_ATTEMPTS - new_attempts
            raise InvalidCodeError(
                f"Incorrect verification code ({rem} attempt{'s' if rem != 1 else ''} remaining)"
            )

        # 5. Success! Move to verified and pin public key
        contact = await self.store.get_contact(peer_id)
        pubkey = contact.pubkey if contact else b""

        # Check key pinning if was previously pinned
        if contact and contact.pinned_at is not None and contact.pubkey != pubkey:
            raise KeyPinningViolationError(
                f"CRITICAL: Key pinning violation for {peer_id}! Refusing verification."
            )

        await self.store.update_contact_state(
            peer_id=peer_id,
            state="verified",
            pinned_at=now_ms,
        )

        # Clean up this session
        await self.store.delete_pairing_session(session.session_id)

        # If peer is local, update peer's contact state too if peer verified
        peer_contact = await self.store.get_contact(local_agent_id)
        if peer_contact and peer_contact.state == "pending_verify":
            # Peer can now be verified
            await self.store.update_contact_state(
                peer_id=local_agent_id,
                state="verified",
                pinned_at=now_ms,
            )

        return {
            "state": "verified",
            "peer_id": peer_id,
            "pinned_at": now_ms,
        }

    async def disconnect(self, local_agent_id: str, peer_id: str) -> bool:
        """Remove a contact and its pinned key."""
        session = await self.store.get_pairing_session_by_peer(local_agent_id, peer_id)
        if session:
            await self.store.delete_pairing_session(session.session_id)
        return await self.store.delete_contact(peer_id)

    async def trust(self, peer_id: str) -> bool:
        """Promote a verified contact to trusted status."""
        contact = await self.store.get_contact(peer_id)
        if not contact:
            raise PairingError(f"Contact {peer_id} does not exist")
        if contact.state != "verified":
            raise PairingError(
                f"Only verified contacts can be trusted (current state: {contact.state})"
            )
        return await self.store.update_contact_state(peer_id, "trusted")

    async def block(self, peer_id: str) -> bool:
        """Block a peer and silently drop all communication."""
        contact = await self.store.get_contact(peer_id)
        pubkey = contact.pubkey if contact else b""
        name = contact.name if contact else f"Agent {peer_id}"

        # Clean up any pairing session
        session = await self.store.get_pairing_session_by_peer("", peer_id)
        if session:
            await self.store.delete_pairing_session(session.session_id)

        await self.store.create_or_update_contact(
            peer_id=peer_id,
            name=name,
            pubkey=pubkey,
            state="blocked",
        )
        return True

    async def unblock(self, peer_id: str) -> bool:
        """Unblock a contact, resetting state to none."""
        contact = await self.store.get_contact(peer_id)
        if not contact or contact.state != "blocked":
            return False
        return await self.store.update_contact_state(peer_id, "none")
