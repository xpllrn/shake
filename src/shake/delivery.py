"""1:1 message delivery, validation, inbox, instant ACK, deadlines, and watchdog."""

import asyncio
import time
from typing import Any

from shake.config import ShakeConfig
from shake.crypto import KeyPair
from shake.protocol import DEFAULT_TIMESTAMP_TOLERANCE_MS, Envelope
from shake.store import Store

MAX_INBOX_SIZE: int = 1000
DEFAULT_REPLY_BUDGET_SECONDS: float = 45.0
HARD_DEADLINE_SECONDS: float = 60.0
DUPLICATE_BODY_WINDOW_MS: int = 60_000


class DeliveryError(Exception):
    """Base delivery exception."""


class RecipientOfflineError(DeliveryError):
    """Target agent has no recent heartbeat and is offline."""


class UntrustedSenderError(DeliveryError):
    """Sender is not a verified or trusted contact."""


class ReplayAttackError(DeliveryError):
    """Nonce was already seen within tolerance window."""


class StaleTimestampError(DeliveryError):
    """Envelope timestamp outside acceptable tolerance."""


class DuplicateMessageError(DeliveryError):
    """Identical body from same sender within 60s or duplicate message ID."""


class InboxFullError(DeliveryError):
    """Recipient inbox has exceeded capacity."""


class ReplyTimeoutError(DeliveryError):
    """Target agent failed to reply within hard deadline."""


class DeliveryManager:
    """Orchestrates message validation, instant ACK, inbox storage, and reply guarantees."""

    def __init__(self, store: Store, config: ShakeConfig | None = None) -> None:
        self.store = store
        self.config = config or ShakeConfig()

    def _load_agent_keypair(self, agent_id: str) -> KeyPair:
        key_path = self.config.key_file_path(agent_id)
        if not key_path.exists():
            raise DeliveryError(f"Private key for agent {agent_id} not found at {key_path}")
        return KeyPair.load(key_path)

    async def send_text(
        self,
        sender_id: str,
        target_id: str,
        body: str,
        wait_timeout: float | None = 60.0,
        check_presence: bool = False,
    ) -> tuple[Envelope, Envelope]:
        """
        Send a text message from sender to target.
        Constructs signed Envelope, verifies recipient status/inbox,
        records outbound copy, delivers to target inbox, generates instant ACK.
        Returns (sent_envelope, ack_envelope).
        """
        now_ms = int(time.time() * 1000)

        # 1. Dedupe check: duplicate body from same sender within 60s
        if await self.store.is_recent_duplicate_body(sender_id, target_id, body):
            raise DuplicateMessageError(
                f"Identical message body sent to {target_id} within 60 seconds."
            )

        # 2. Check if sender exists and load keypair
        sender_keypair = self._load_agent_keypair(sender_id)

        # 3. Create and sign envelope
        envelope = Envelope.create(
            type_="text",
            from_id=sender_id,
            to_id=target_id,
            body=body,
            ts=now_ms,
            reply_by=now_ms + int((wait_timeout or HARD_DEADLINE_SECONDS) * 1000),
        )
        envelope.sign(sender_keypair)

        # 4. Save outbound message for sender
        await self.store.save_message(
            owner_agent_id=sender_id,
            msg_id=envelope.msg_id,
            envelope=envelope.encode(),
            direction="outbound",
            state="sent",
            sender_id=sender_id,
            target_id=target_id,
            ts=envelope.ts,
            reply_to=envelope.reply_to,
            reply_by=envelope.reply_by,
            body=str(body),
        )

        # 5. Process delivery at recipient side
        ack_envelope = await self.receive_envelope(envelope, check_presence=check_presence)

        # 6. Mark outbound message as acked
        await self.store.update_message_state(sender_id, envelope.msg_id, "acked")

        return envelope, ack_envelope

    async def receive_envelope(
        self,
        envelope: Envelope,
        check_presence: bool = False,
    ) -> Envelope:
        """
        Validate incoming envelope for recipient, enforce contacts/signatures,
        store in recipient's inbox, and return an instant signed ACK envelope.
        """
        now_ms = int(time.time() * 1000)
        target_id = envelope.to_id
        sender_id = envelope.from_id

        # 1. Target presence check
        target_agent = await self.store.get_agent(target_id)
        if not target_agent:
            raise DeliveryError(f"Target agent {target_id} not registered locally.")

        if check_presence:
            hb = await self.store.get_heartbeat(target_id)
            if hb is not None:
                is_online = (now_ms - hb.last_seen) <= 30_000 and hb.status == "online"
                if not is_online:
                    delta = now_ms - hb.last_seen
                    raise RecipientOfflineError(
                        f"Target agent {target_id} is offline (last seen {delta}ms ago). Fail fast."
                    )

        # 2. Sender must be verified or trusted contact of target
        contact = await self.store.get_contact(target_id, sender_id)
        if not contact or contact.state not in ("verified", "trusted"):
            raise UntrustedSenderError(
                f"Sender {sender_id} is not a verified contact of {target_id} "
                f"(state: {contact.state if contact else 'none'})."
            )

        # 3. Signature verification with pinned pubkey
        if not contact.pubkey or not envelope.verify(contact.pubkey):
            raise DeliveryError(
                f"Invalid cryptographic signature on envelope from {sender_id}."
            )

        # 4. Timestamp check (tolerance 5 min)
        if not envelope.validate_timestamp(now_ms, tolerance_ms=DEFAULT_TIMESTAMP_TOLERANCE_MS):
            raise StaleTimestampError(
                f"Timestamp {envelope.ts} outside tolerance window (current: {now_ms})."
            )

        # 5. Replay nonce check
        if await self.store.is_nonce_seen(envelope.nonce):
            raise ReplayAttackError(
                f"Replay attack detected: nonce {envelope.nonce.hex()[:12]} already seen."
            )
        await self.store.record_nonce(envelope.nonce, sender_id)

        # 6. Inbox capacity check
        inbox_count = await self.store.count_inbox(target_id)
        if inbox_count >= MAX_INBOX_SIZE:
            target_keypair = self._load_agent_keypair(target_id)
            busy_env = Envelope.create(
                type_="status",
                from_id=target_id,
                to_id=sender_id,
                body="Inbox capacity exceeded",
                reply_to=envelope.msg_id,
                status="busy",
            )
            busy_env.sign(target_keypair)
            raise InboxFullError(f"Inbox for {target_id} is full ({inbox_count} messages).")

        # 7. Check for duplicate msg_id in target's store (idempotent retry)
        existing_msg = await self.store.get_message(target_id, envelope.msg_id)
        if existing_msg:
            target_keypair = self._load_agent_keypair(target_id)
            ack_env = Envelope.create(
                type_="ack",
                from_id=target_id,
                to_id=sender_id,
                body="",
                reply_to=envelope.msg_id,
                status="ok",
            )
            ack_env.sign(target_keypair)
            return ack_env

        # 8. Save inbound message in target's inbox
        body_text = (
            envelope.body
            if isinstance(envelope.body, str)
            else envelope.body.decode("utf-8", errors="replace")
        )
        await self.store.save_message(
            owner_agent_id=target_id,
            msg_id=envelope.msg_id,
            envelope=envelope.encode(),
            direction="inbound",
            state="delivered",
            sender_id=sender_id,
            target_id=target_id,
            ts=envelope.ts,
            reply_to=envelope.reply_to,
            reply_by=envelope.reply_by,
            body=body_text,
        )

        # 9. Generate INSTANT ACK signed by target daemon
        target_keypair = self._load_agent_keypair(target_id)
        ack_env = Envelope.create(
            type_="ack",
            from_id=target_id,
            to_id=sender_id,
            body="",
            reply_to=envelope.msg_id,
            status="ok",
        )
        ack_env.sign(target_keypair)
        return ack_env

    async def execute_adapter_turn(
        self,
        inbound_envelope: Envelope,
        adapter: Any,
        context: dict[str, Any] | None = None,
    ) -> Envelope | None:
        """
        Execute agent adapter with budget: min(45s, reply_by - now - 5s).
        Sends fallback reply on timeout (status='timeout').
        If adapter returns NOOP, it is not posted.
        Addresses reply back to sender if not addressed.
        """
        now_ms = int(time.time() * 1000)
        target_id = inbound_envelope.to_id
        sender_id = inbound_envelope.from_id

        # Calculate budget
        if inbound_envelope.reply_by:
            remaining_ms = inbound_envelope.reply_by - now_ms - 5000
            budget_seconds = max(1.0, min(DEFAULT_REPLY_BUDGET_SECONDS, remaining_ms / 1000.0))
        else:
            budget_seconds = DEFAULT_REPLY_BUDGET_SECONDS

        target_keypair = self._load_agent_keypair(target_id)

        try:
            async def run_adapter() -> str:
                chunks: list[str] = []
                async for chunk in adapter.handle(inbound_envelope, context or {}):
                    chunks.append(chunk)
                return "".join(chunks).strip()

            reply_text = await asyncio.wait_for(run_adapter(), timeout=budget_seconds)

            # Check for NOOP
            if reply_text.upper() == "NOOP" or not reply_text:
                return None

            # Construct reply envelope
            reply_envelope = Envelope.create(
                type_="reply",
                from_id=target_id,
                to_id=sender_id,
                body=reply_text,
                reply_to=inbound_envelope.msg_id,
                status="ok",
            )
            reply_envelope.sign(target_keypair)

            # Store outbound reply for target
            await self.store.save_message(
                owner_agent_id=target_id,
                msg_id=reply_envelope.msg_id,
                envelope=reply_envelope.encode(),
                direction="outbound",
                state="sent",
                sender_id=target_id,
                target_id=sender_id,
                ts=reply_envelope.ts,
                reply_to=reply_envelope.reply_to,
                reply_by=None,
                body=reply_text,
            )

            # Store inbound reply for sender
            await self.store.save_message(
                owner_agent_id=sender_id,
                msg_id=reply_envelope.msg_id,
                envelope=reply_envelope.encode(),
                direction="inbound",
                state="delivered",
                sender_id=target_id,
                target_id=sender_id,
                ts=reply_envelope.ts,
                reply_to=reply_envelope.reply_to,
                reply_by=None,
                body=reply_text,
            )

            # Update original message state to 'replied'
            await self.store.update_message_state(target_id, inbound_envelope.msg_id, "replied")
            await self.store.update_message_state(sender_id, inbound_envelope.msg_id, "replied")

            return reply_envelope

        except TimeoutError:
            # Fallback reply sent by daemon on timeout
            timeout_env = Envelope.create(
                type_="reply",
                from_id=target_id,
                to_id=sender_id,
                body="[shake: timeout] Agent did not reply within budget.",
                reply_to=inbound_envelope.msg_id,
                status="timeout",
            )
            timeout_env.sign(target_keypair)

            await self.store.save_message(
                owner_agent_id=target_id,
                msg_id=timeout_env.msg_id,
                envelope=timeout_env.encode(),
                direction="outbound",
                state="timeout",
                sender_id=target_id,
                target_id=sender_id,
                ts=timeout_env.ts,
                reply_to=inbound_envelope.msg_id,
                body=str(timeout_env.body),
            )
            await self.store.save_message(
                owner_agent_id=sender_id,
                msg_id=timeout_env.msg_id,
                envelope=timeout_env.encode(),
                direction="inbound",
                state="timeout",
                sender_id=target_id,
                target_id=sender_id,
                ts=timeout_env.ts,
                reply_to=inbound_envelope.msg_id,
                body=str(timeout_env.body),
            )
            await self.store.update_message_state(target_id, inbound_envelope.msg_id, "timeout")
            await self.store.update_message_state(sender_id, inbound_envelope.msg_id, "timeout")
            return timeout_env
