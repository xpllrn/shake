"""Unit and integration tests for 1:1 message delivery, instant ACK, deadlines, and watchdog."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from shake.adapters.base import Adapter
from shake.config import ShakeConfig
from shake.crypto import KeyPair
from shake.delivery import (
    DeliveryManager,
    DuplicateMessageError,
    RecipientOfflineError,
    ReplayAttackError,
    StaleTimestampError,
    UntrustedSenderError,
)
from shake.pairing import PairingManager
from shake.protocol import Envelope
from shake.store import Store


class MockEchoAdapter(Adapter):
    """Echoes message back with prefix."""

    async def handle(
        self, envelope: Envelope, context: dict[str, Any]
    ) -> AsyncIterator[str]:
        text = envelope.body if isinstance(envelope.body, str) else envelope.body.decode()
        yield f"Echo: {text}"

    async def heartbeat(self) -> None:
        pass


class MockSlowAdapter(Adapter):
    """Simulates hanging model that hits timeout."""

    async def handle(
        self, envelope: Envelope, context: dict[str, Any]
    ) -> AsyncIterator[str]:
        await asyncio.sleep(5.0)  # Will exceed budget
        yield "Too late"

    async def heartbeat(self) -> None:
        pass


class MockNoopAdapter(Adapter):
    """Returns NOOP."""

    async def handle(
        self, envelope: Envelope, context: dict[str, Any]
    ) -> AsyncIterator[str]:
        yield "NOOP"

    async def heartbeat(self) -> None:
        pass


@pytest.fixture
async def delivery_env(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    config = ShakeConfig(home=tmp_path)
    config.ensure_directories()
    store = Store(config.db_path)
    await store.open()

    # Create Alice & Bob
    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    kp_a.save(config.key_file_path("1111111"))
    kp_b.save(config.key_file_path("2222222"))
    await store.create_agent("1111111", "Alice", kp_a.public_bytes)
    await store.create_agent("2222222", "Bob", kp_b.public_bytes)

    # Establish verified mutual pairing
    pm = PairingManager(store, config)
    res_a = await pm.initiate_connect("1111111", "2222222")
    code = res_a["code"]
    await pm.verify_code("1111111", "2222222", code)
    await pm.verify_code("2222222", "1111111", code)

    dm = DeliveryManager(store, config)
    try:
        yield {
            "config": config,
            "store": store,
            "dm": dm,
            "alice_id": "1111111",
            "bob_id": "2222222",
            "kp_a": kp_a,
            "kp_b": kp_b,
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_send_and_instant_ack(delivery_env: dict[str, Any]) -> None:
    dm: DeliveryManager = delivery_env["dm"]
    store: Store = delivery_env["store"]
    alice_id = delivery_env["alice_id"]
    bob_id = delivery_env["bob_id"]

    # Alice sends text to Bob
    sent_env, ack_env = await dm.send_text(alice_id, bob_id, "Hello Bob!")

    assert sent_env.from_id == alice_id
    assert sent_env.to_id == bob_id
    assert sent_env.type == "text"

    # ACK is instant, status ok, reply_to matches
    assert ack_env.type == "ack"
    assert ack_env.status == "ok"
    assert ack_env.from_id == bob_id
    assert ack_env.reply_to == sent_env.msg_id

    # Bob's inbox has the message
    inbox = await store.get_inbox(bob_id)
    assert len(inbox) == 1
    assert inbox[0].msg_id == sent_env.msg_id
    assert inbox[0].body == "Hello Bob!"
    assert inbox[0].state == "delivered"

    # Alice's outbox has the message marked acked
    out_msg = await store.get_message(alice_id, sent_env.msg_id)
    assert out_msg is not None
    assert out_msg.state == "acked"


@pytest.mark.asyncio
async def test_untrusted_sender_rejected(delivery_env: dict[str, Any]) -> None:
    dm: DeliveryManager = delivery_env["dm"]
    store: Store = delivery_env["store"]
    bob_id = delivery_env["bob_id"]
    config: ShakeConfig = delivery_env["config"]

    # Create Mallory (unpaired)
    kp_m = KeyPair.generate()
    kp_m.save(config.key_file_path("3333333"))
    await store.create_agent("3333333", "Mallory", kp_m.public_bytes)

    # Mallory tries to send to Bob
    with pytest.raises(UntrustedSenderError):
        await dm.send_text("3333333", bob_id, "Attack!")


@pytest.mark.asyncio
async def test_duplicate_body_rejected_within_60s(delivery_env: dict[str, Any]) -> None:
    dm: DeliveryManager = delivery_env["dm"]
    alice_id = delivery_env["alice_id"]
    bob_id = delivery_env["bob_id"]

    await dm.send_text(alice_id, bob_id, "Repeat message")

    with pytest.raises(DuplicateMessageError):
        await dm.send_text(alice_id, bob_id, "Repeat message")


@pytest.mark.asyncio
async def test_replay_attack_rejected(delivery_env: dict[str, Any]) -> None:
    dm: DeliveryManager = delivery_env["dm"]
    alice_id = delivery_env["alice_id"]
    bob_id = delivery_env["bob_id"]

    # First send succeeds
    sent_env, _ = await dm.send_text(alice_id, bob_id, "Unique nonce test 1")

    # Trying to replay exact same envelope
    with pytest.raises(ReplayAttackError):
        await dm.receive_envelope(sent_env)


@pytest.mark.asyncio
async def test_stale_timestamp_rejected(delivery_env: dict[str, Any]) -> None:
    dm: DeliveryManager = delivery_env["dm"]
    alice_id = delivery_env["alice_id"]
    bob_id = delivery_env["bob_id"]
    kp_a: KeyPair = delivery_env["kp_a"]

    # Construct envelope from 10 minutes ago
    ten_min_ago = int((asyncio.get_event_loop().time() - 600) * 1000)
    stale_env = Envelope.create(
        type_="text",
        from_id=alice_id,
        to_id=bob_id,
        body="Old message",
        ts=ten_min_ago,
    )
    stale_env.sign(kp_a)

    with pytest.raises(StaleTimestampError):
        await dm.receive_envelope(stale_env)


@pytest.mark.asyncio
async def test_adapter_turn_execution_and_noop(delivery_env: dict[str, Any]) -> None:
    dm: DeliveryManager = delivery_env["dm"]
    store: Store = delivery_env["store"]
    alice_id = delivery_env["alice_id"]
    bob_id = delivery_env["bob_id"]

    # Alice sends message
    sent_env, _ = await dm.send_text(alice_id, bob_id, "What is your status?")

    # Bob's echo adapter runs
    echo_adapter = MockEchoAdapter()
    reply = await dm.execute_adapter_turn(sent_env, echo_adapter, {"peer_name": "Alice"})
    assert reply is not None
    assert reply.body == "Echo: What is your status?"
    assert reply.from_id == bob_id
    assert reply.to_id == alice_id

    # Verify conversation history on both sides
    hist_alice = await store.get_history(alice_id, bob_id)
    assert len(hist_alice) == 2  # sent message + reply
    assert hist_alice[0].body == "What is your status?"
    assert hist_alice[1].body == "Echo: What is your status?"

    # Test NOOP adapter
    sent_env2, _ = await dm.send_text(alice_id, bob_id, "Are you there?")
    noop_adapter = MockNoopAdapter()
    reply_noop = await dm.execute_adapter_turn(sent_env2, noop_adapter)
    assert reply_noop is None  # NOOP is never posted


@pytest.mark.asyncio
async def test_watchdog_timeout_fallback(delivery_env: dict[str, Any]) -> None:
    dm: DeliveryManager = delivery_env["dm"]
    alice_id = delivery_env["alice_id"]
    bob_id = delivery_env["bob_id"]
    kp_a: KeyPair = delivery_env["kp_a"]

    # Create envelope with tight deadline: reply_by = now + 1.5s
    now_ms = int(asyncio.get_event_loop().time() * 1000)
    tight_env = Envelope.create(
        type_="text",
        from_id=alice_id,
        to_id=bob_id,
        body="Fast answer please",
        reply_by=now_ms + 1500,  # 1.5s
    )
    tight_env.sign(kp_a)
    await dm.receive_envelope(tight_env)

    # Execute with slow adapter
    slow_adapter = MockSlowAdapter()
    reply = await dm.execute_adapter_turn(tight_env, slow_adapter)

    # Daemon generates fallback reply with status: 'timeout'
    assert reply is not None
    assert reply.status == "timeout"
    assert "[shake: timeout]" in str(reply.body)


@pytest.mark.asyncio
async def test_offline_recipient_fails_fast(delivery_env: dict[str, Any]) -> None:
    dm: DeliveryManager = delivery_env["dm"]
    store: Store = delivery_env["store"]
    alice_id = delivery_env["alice_id"]
    bob_id = delivery_env["bob_id"]

    # Record stale heartbeat for Bob (40 seconds ago)
    past_ms = int((asyncio.get_event_loop().time() - 40) * 1000)
    await store.db.execute(
        "INSERT OR REPLACE INTO heartbeats (agent_id, last_seen, status) VALUES (?, ?, 'online');",
        (bob_id, past_ms),
    )
    await store.db.commit()

    # Sending with check_presence=True should fail fast
    with pytest.raises(RecipientOfflineError):
        await dm.send_text(alice_id, bob_id, "Are you online?", check_presence=True)
